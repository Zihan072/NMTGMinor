import torch
import torch.nn as nn
from onmt.models.transformer_layers import PositionalEncoding, PrePostProcessing
from onmt.models.transformer_layers import EncoderLayer, DecoderLayer
from onmt.models.transformers import TransformerEncoder, TransformerDecoder, Transformer, TransformerDecodingState
import onmt
from onmt.modules.base_seq2seq import NMTModel, Reconstructor, DecoderState
from onmt.modules.dropout import embedded_dropout
from onmt.models.transformer_layers import XavierLinear, MultiHeadAttention, FeedForward, PrePostProcessing
from onmt.models.relative_transformer_layers import RelativeTransformerEncoderLayer, RelativeTransformerDecoderLayer
from onmt.utils import flip, expected_length
from collections import defaultdict
import math

torch.set_printoptions(threshold=500000)


#  Positional Embedding with discrete inputs
class SinusoidalPositionalEmbedding(nn.Module):
    def __init__(self, demb):
        super(SinusoidalPositionalEmbedding, self).__init__()

        self.demb = demb

        inv_freq = 1 / (10000 ** (torch.arange(0.0, demb, 2.0) / demb))
        self.register_buffer('inv_freq', inv_freq)

    def forward(self, pos_seq, sin_first=True, bsz=None):
        """
        :param bsz:
        :param pos_seq: sequences of RELATIVE position indices (can be negative for future)
        :param sin_first: in Attention is all you need paper, sin is first then cosin
        """
        sinusoid_inp = torch.ger(pos_seq, self.inv_freq)

        if sin_first:
            pos_emb = torch.cat([sinusoid_inp.sin(), sinusoid_inp.cos()], dim=-1)
        else:
            pos_emb = torch.cat([sinusoid_inp.cos(), sinusoid_inp.sin()], dim=-1)

        if bsz is not None:
            return pos_emb[:, None, :].expand(-1, bsz, -1)
        else:
            return pos_emb[:, None, :]


class LearnablePostionEmbedding(nn.Module):

    def __init__(self, max_pos, demb):
        super(LearnablePostionEmbedding, self).__init__()
        self.max_pos = max(max_pos, 5000)
        # self.embedding = nn.Embedding(2 * max_pos + 1, demb)
        self.embedding = nn.Embedding(self.max_pos, demb)

    def forward(self, input):
        # pos = torch.clamp(input, 0, self.max_pos)
        # k = min((pos.size(0) - 1) // 2, self.max_pos)
        return self.embedding(input)


class RelativeTransformerEncoder(TransformerEncoder):

    def __init__(self, opt, dicts, positional_encoder, encoder_type='text', language_embeddings=None):
        self.death_rate = opt.death_rate
        self.double_position = opt.double_position
        self.learnable_position_encoding = opt.learnable_position_encoding
        self.layer_modules = list()
        self.asynchronous = opt.asynchronous
        self.max_memory_size = opt.max_memory_size

        # build_modules will be called from the inherited constructor
        super(RelativeTransformerEncoder, self).__init__(opt, dicts, positional_encoder, encoder_type,
                                                         language_embeddings)

        # learnable position encoding
        if self.learnable_position_encoding:
            self.max_pos_length = opt.max_pos_length
            # pos_emb = self.model_size // self.n_heads
            pos_emb = self.model_size
            self.positional_encoder = LearnablePostionEmbedding(self.max_pos_length, pos_emb)
            print("* Learnable position encoding with max %d positions" % self.max_pos_length)
        else:
            # or using pre-set sinusoidal
            self.positional_encoder = SinusoidalPositionalEmbedding(opt.model_size)

        self.d_head = self.model_size // self.n_heads

    def build_modules(self):

        e_length = expected_length(self.layers, self.death_rate)
        print("* Transformer Encoder with Relative Attention with %.2f expected layers" % e_length)

        self.layer_modules = nn.ModuleList()

        for _l in range(self.layers):
            # linearly decay the death rate
            death_r = (_l + 1.0) / self.layers * self.death_rate

            block = RelativeTransformerEncoderLayer(self.n_heads, self.model_size,
                                                    self.dropout, self.inner_size, self.attn_dropout,
                                                    variational=self.varitional_dropout, death_rate=death_r)

            self.layer_modules.append(block)

    def create_stream_mask(self, input, input_length, prev_mem_size):

        lengths = input_length.tolist()
        mask = None

        for length in lengths:

            # the current mask should be either None or

            if mask is None:
                prev_length = 0
            else:
                prev_length = mask.size(1)

            # n current queries attend to n + p keys
            current_mask = input.new_zeros(length, length + prev_length)

            if prev_length > 0:
                prev_mask = input.new_ones(prev_length, length)
                prev_mask = torch.cat([mask, prev_mask], dim=-1)
            else:
                prev_mask = None

            if prev_mask is not None:
                mask = torch.cat([prev_mask, current_mask], dim=0)
            else:
                mask = current_mask

        if prev_mem_size > 0:
            # all current elements attend to all buffer elements
            buffer_mask = mask.new_zeros(mask.size(0), prev_mem_size)
            mask = torch.cat([buffer_mask, mask], dim=-1)

        mask = mask.bool()

        return mask
        # raise NotImplementedError

    def forward_stream(self, input, input_pos, input_lang, **kwargs):
        input_length = kwargs.get('src_lengths', None)
        streaming_state = kwargs.get('streaming_state', None)
        input = input.transpose(0, 1)

        mask_src = self.create_stream_mask(input, input_length, streaming_state.prev_src_mem_size)
        mask_src = mask_src.unsqueeze(2)

        emb = embedded_dropout(self.word_lut, input, dropout=self.word_dropout if self.training else 0)  #

        """ Adding language embeddings """
        if self.use_language_embedding:
            assert self.language_embedding is not None
            # There is no "unsqueeze" here because the input is T x B x H and lang_emb is B x H
            if self.language_embedding_type in ['sum', 'all_sum']:
                lang_emb = self.language_embedding(input_lang)
                emb = emb + lang_emb.unsqueeze(1)

        """ Scale the emb by sqrt(d_model) """
        emb = emb * math.sqrt(self.model_size)

        klen = input.size(0) + streaming_state.prev_src_mem_size

        pos = torch.arange(klen - 1, -klen, -1.0, device=emb.device, dtype=emb.dtype)

        pos_emb = self.positional_encoder(pos)

        context = emb

        # Apply dropout to both context and pos_emb
        context = self.preprocess_layer(context)

        pos_emb = self.preprocess_layer(pos_emb)

        for i, layer in enumerate(self.layer_modules):
            # src_len x batch_size x d_model
            buffer = streaming_state.src_buffer[i]
            context, buffer = layer(context, pos_emb, mask_src, incremental=True, incremental_cache=buffer)
            streaming_state.src_buffer[i] = buffer

        # last layer normalization
        context = self.postprocess_layer(context)

        output_dict = defaultdict(lambda: None, {'context': context, 'src_mask': mask_src, 'src': input})
        output_dict['streaming_state'] = streaming_state
        streaming_state.prev_src_mem_size += sum(input_length.tolist())

        streaming_state.prune_source_memory(self.max_memory_size)

        return output_dict

    def forward(self, input, input_pos=None, input_lang=None, streaming=False, **kwargs):
        """
        Inputs Shapes:
            input: batch_size x src_len (wanna tranpose)
        Outputs Shapes:
            out: batch_size x src_len x d_model
            mask_src
        """
        if streaming:
            return self.forward_stream(input, input_pos, input_lang, **kwargs)

        """ Embedding: batch_size x src_len x d_model """
        if self.input_type == "text":
            bsz_first_input = input
            input = input.transpose(0, 1)
            # mask_src = input.eq(onmt.constants.PAD).unsqueeze(1)  # batch_size x src_len x 1 for broadcasting
            mask_src = input.eq(onmt.constants.PAD).unsqueeze(0)
            dec_attn_mask = bsz_first_input.eq(onmt.constants.PAD).unsqueeze(1)

            emb = embedded_dropout(self.word_lut, input, dropout=self.word_dropout if self.training else 0)

            if self.double_position:
                assert input_pos is not None
                # flatten
                src_len, bsz = input_pos.size(0), input_pos.size(1)
                input_pos_ = input_pos.contiguous().view(-1).type_as(emb)
                abs_pos = self.positional_encoder(input_pos_)
                abs_pos = abs_pos.squeeze(1).view(src_len, bsz, -1)

            else:
                abs_pos = None

            """ Adding language embeddings """
            if self.use_language_embedding:
                assert self.language_embedding is not None
                # There is no "unsqueeze" here because the input is T x B x H and lang_emb is B x H
                if self.language_embedding_type in ['sum', 'all_sum']:
                    lang_emb = self.language_embedding(input_lang)
                    emb = emb + lang_emb.unsqueeze(1)

        else:
            if not self.cnn_downsampling:
                mask_src = input.narrow(2, 0, 1).squeeze(2).transpose(0, 1).eq(onmt.constants.PAD).unsqueeze(0)
                dec_attn_mask = input.narrow(2, 0, 1).squeeze(2).eq(onmt.constants.PAD).unsqueeze(1)
                input = input.narrow(2, 1, input.size(2) - 1)
                emb = self.audio_trans(input.contiguous().view(-1, input.size(2))).view(input.size(0),
                                                                                        input.size(1), -1)
            else:
                long_mask = input.narrow(2, 0, 1).squeeze(2).eq(onmt.constants.PAD)
                input = input.narrow(2, 1, input.size(2) - 1)

                # first resizing to fit the CNN format
                input = input.view(input.size(0), input.size(1), -1, self.channels)
                input = input.permute(0, 3, 1, 2)

                input = self.audio_trans(input)
                input = input.permute(0, 2, 1, 3).contiguous()
                input = input.view(input.size(0), input.size(1), -1)
                # print(input.size())
                input = self.linear_trans(input)

                mask_src = long_mask[:, 0:input.size(1) * 4:4].transpose().unsqueeze(0)
                dec_attn_mask = long_mask[:, 0:input.size(1) * 4:4].unsqueeze(1)
                # the size seems to be B x T ?
                emb = input

            emb = emb.transpose(0, 1)
            input = input.transpose(0, 1)
            abs_pos = None

        if onmt.constants.torch_version >= 1.2:
            mask_src = mask_src.bool()

        """ Scale the emb by sqrt(d_model) """
        emb = emb * math.sqrt(self.model_size)

        if self.double_position and abs_pos is not None:
            # adding position encoding
            emb = emb + abs_pos

        """ Adding positional encoding """
        klen = input.size(0)

        # Asynchronous positions: 2K+1 positions instead of K+1
        if self.asynchronous:
            pos = torch.arange(klen - 1, -klen, -1.0, device=emb.device, dtype=emb.dtype)
        else:
            # Everything should be asynchronous now
            pos = torch.arange(klen - 1, -klen, -1.0, device=emb.device, dtype=emb.dtype)
            # pos = torch.arange(klen - 1, -1, -1.0, device=emb.device, dtype=emb.dtype)

        pos_emb = self.positional_encoder(pos)

        if self.learnable_position_encoding:
            raise NotImplementedError

        # if not self.learnable_position_encoding:
        #     # L x 1 x H
        #     past_pos = pos[:klen+1]
        #     future_pos = pos[-klen:]
        #     past_pos_emb = self.positional_encoder(past_pos, sin_first=True)
        #     future_pos_emb = self.positional_encoder(future_pos, sin_first=False)
        #     pos_emb = torch.cat([past_pos_emb, future_pos_emb], dim=0)
        # else:
        #     raise NotImplementedError
        #     pos = pos.long()
        #     # pos = torch.arange(klen - 1, -klen, -1.0, device=emb.device, dtype=input.dtype)
        #     # clamp the positions (all postions from afar are treated equally, maybe?)
        #     # (2L-1) x 1 x H
        #     pos_emb = self.positional_encoder(pos.unsqueeze(1))
        #     # print(pos_emb.size())

        # B x T x H -> T x B x H
        context = emb

        # Apply dropout to both context and pos_emb
        context = self.preprocess_layer(context)

        pos_emb = self.preprocess_layer(pos_emb)

        # print(context.size(), mask_src.size())

        for i, layer in enumerate(self.layer_modules):
            # src_len x batch_size x d_model
            context = layer(context, pos_emb, mask_src)

        # From Google T2T
        # if normalization is done in layer_preprocess, then it should also be done
        # on the output, since the output can grow very large, being the sum of
        # a whole stack of unnormalized layer outputs.
        context = self.postprocess_layer(context)

        output_dict = defaultdict(lambda: None, {'context': context, 'src_mask': dec_attn_mask, 'src': input})

        return output_dict


class RelativeTransformerDecoder(TransformerDecoder):

    def __init__(self, opt, dicts, positional_encoder, language_embeddings=None, ignore_source=False):

        self.death_rate = opt.death_rate
        self.double_position = opt.double_position
        self.max_memory_size = opt.max_memory_size
        self.stream_context = opt.stream_context

        # build_modules will be called from the inherited constructor
        super(RelativeTransformerDecoder, self).__init__(opt, dicts,
                                                         positional_encoder,
                                                         language_embeddings,
                                                         ignore_source,
                                                         allocate_positions=False)
        self.positional_encoder = SinusoidalPositionalEmbedding(opt.model_size)
        self.d_head = self.model_size // self.n_heads
        # Parameters for the position biases
        self.r_w_bias = nn.Parameter(torch.Tensor(self.n_heads, self.d_head))
        self.r_r_bias = nn.Parameter(torch.Tensor(self.n_heads, self.d_head))

    def renew_buffer(self, new_len):
        return

    def build_modules(self):

        e_length = expected_length(self.layers, self.death_rate)

        print("* Transformer Decoder with Relative Attention with %.2f expected layers" % e_length)

        self.layer_modules = nn.ModuleList()

        for l in range(self.layers):
            # linearly decay the death rate
            death_r = (l + 1.0) / self.layers * self.death_rate

            block = RelativeTransformerDecoderLayer(self.n_heads, self.model_size,
                                                    self.dropout, self.inner_size, self.attn_dropout,
                                                    variational=self.variational_dropout, death_rate=death_r)

            self.layer_modules.append(block)

    def process_embedding(self, input, input_lang=None):

        return input

    def create_context_mask(self, input, src, src_lengths, tgt_lengths):
        """
        Generate the mask so that part of the target attends to a part of the source
        :param input:
        :param src:
        :param src_lengths:
        :param tgt_lengths:
        :return:
        """

        mask = None

        if self.stream_context == 'global':
            # Global context: one target attends to everything in the source
            for (src_length, tgt_length) in zip(src_lengths, tgt_lengths):

                if mask is None:
                    prev_src_length = 0
                    prev_tgt_length = 0
                else:
                    prev_src_length, prev_tgt_length = mask.size(1), mask.size(0)

                # current sent attend to current src sent and all src in the past
                current_mask = input.new_zeros(tgt_length, src_length + prev_src_length)

                # the previous target cannot attend to the current source
                if prev_tgt_length > 0:
                    prev_mask = input.new_ones(prev_tgt_length, src_length)
                    prev_mask = torch.cat([mask, prev_mask], dim=-1)
                else:
                    prev_mask = None

                # the output mask has two parts: the prev and the current
                if prev_mask is not None:
                    mask = torch.cat([prev_mask, current_mask], dim=0)
                else:
                    mask = current_mask

        elif self.stream_context == 'local':
            # Local context: only attends to the aligned context
            for (src_length, tgt_length) in zip(src_lengths, tgt_lengths):

                if mask is None:
                    prev_src_length = 0
                    prev_tgt_length = 0
                else:
                    prev_src_length, prev_tgt_length = mask.size(1), mask.size(0)

                # current tgt sent attend to only current src sent
                if prev_src_length > 0:
                    current_mask = torch.cat([input.new_ones(tgt_length, prev_src_length),
                                              input.new_zeros(tgt_length, src_length)], dim=-1)
                else:
                    current_mask = input.new_zeros(tgt_length, src_length)

                # the previous target cannot attend to the current source
                if prev_tgt_length > 0:
                    prev_mask = input.new_ones(prev_tgt_length, src_length)
                    prev_mask = torch.cat([mask, prev_mask], dim=-1)
                else:
                    prev_mask = None

                # the output mask has two parts: the prev and the current
                if prev_mask is not None:
                    mask = torch.cat([prev_mask, current_mask], dim=0)
                else:
                    mask = current_mask
        else:
            raise NotImplementedError

        mask = mask.bool()

        return mask

    def create_self_attn_mask(self, input, tgt_lengths, prev_tgt_mem_size):
        """
        Create a mask for the target words attending to the past
        :param input:
        :param tgt_lengths:
        :param prev_tgt_mem_size:
        :return:
        """

        qlen = sum(tgt_lengths.tolist())
        mlen = prev_tgt_mem_size
        klen = qlen + mlen

        mask = torch.triu(input.new_ones(qlen, klen), diagonal=1 + mlen).bool()[:, :, None]

        return mask

    def forward_stream(self, input, context, src, input_pos, input_lang, **kwargs):

        input = input.transpose(0, 1)
        src_lengths = kwargs.get("src_lengths", None)
        tgt_lengths = kwargs.get("tgt_lengths", None)
        streaming_state = kwargs.get("streaming_state")

        emb = embedded_dropout(self.word_lut, input, dropout=self.word_dropout if self.training else 0)
        emb = emb * math.sqrt(self.model_size)

        if self.use_language_embedding:
            lang_emb = self.language_embeddings(input_lang)  # B x H or 1 x H
            if self.language_embedding_type == 'sum':
                emb = emb + lang_emb
            elif self.language_embedding_type == 'concat':
                # replace the bos embedding with the language
                bos_emb = lang_emb.expand_as(emb[0])
                emb[0] = bos_emb

                lang_emb = lang_emb.unsqueeze(0).expand_as(emb)
                concat_emb = torch.cat([emb, lang_emb], dim=-1)
                emb = torch.relu(self.projector(concat_emb))
            else:
                raise NotImplementedError

        if context is not None:
            context_attn_mask = self.create_context_mask(input, src, src_lengths, tgt_lengths)
            context_attn_mask = context_attn_mask.unsqueeze(0)
        else:
            context_attn_mask = None

        dec_attn_mask = self.create_self_attn_mask(input, tgt_lengths, streaming_state.prev_tgt_mem_size)

        klen = input.size(0) + streaming_state.prev_tgt_mem_size
        pos = torch.arange(klen - 1, -1, -1.0, device=emb.device, dtype=emb.dtype)

        pos_emb = self.positional_encoder(pos)

        output = self.preprocess_layer(emb)

        for i, layer in enumerate(self.layer_modules):
            # batch_size x src_len x d_model
            buffer = streaming_state.tgt_buffer[i]
            # output, coverage = layer(output, context, pos_emb, self.r_w_bias, self.r_r_bias, dec_attn_mask, mask_src)
            output, coverage, buffer = layer(output, context, pos_emb, dec_attn_mask, context_attn_mask,
                                             incremental=True, incremental_cache=buffer, reuse_source=False)
            streaming_state.tgt_buffer[i] = buffer

        output = self.postprocess_layer(output)

        streaming_state.prev_tgt_mem_size += sum(tgt_lengths.tolist())
        streaming_state.prune_target_memory(self.max_memory_size)

        output_dict = defaultdict(lambda: None, {'hidden': output, 'coverage': coverage, 'context': context})
        output_dict['streaming_state'] = streaming_state

        return output_dict

    def forward(self, input, context, src, input_pos=None, input_lang=None, streaming=False, **kwargs):
        """
                Inputs Shapes:
                    input: (Variable) batch_size x len_tgt (wanna tranpose)
                    context: (Variable) batch_size x src_len x d_model
                    mask_src (Tensor) batch_size x src_len
                Outputs Shapes:
                    out: batch_size x len_tgt x d_model
                    coverage: batch_size x len_tgt x src_len

                """
        if streaming:
            return self.forward_stream(input, context, src, input_pos, input_lang, **kwargs)

        """ Embedding: batch_size x len_tgt x d_model """
        input = input.transpose(0, 1)  # T x B
        emb = embedded_dropout(self.word_lut, input, dropout=self.word_dropout if self.training else 0)
        emb = emb * math.sqrt(self.model_size)

        if self.double_position:
            assert input_pos is not None
            tgt_len, bsz = input_pos.size(0), input_pos.size(1)
            input_pos_ = input_pos.view(-1).type_as(emb)
            abs_pos = self.positional_encoder(input_pos_).squeeze(1).view(tgt_len, bsz, -1)

            emb = emb + abs_pos

        if self.use_language_embedding:
            lang_emb = self.language_embeddings(input_lang)  # B x H or 1 x H
            if self.language_embedding_type == 'sum':
                emb = emb + lang_emb
            elif self.language_embedding_type == 'concat':
                # replace the bos embedding with the language
                bos_emb = lang_emb.expand_as(emb[0])
                emb[0] = bos_emb

                lang_emb = lang_emb.unsqueeze(0).expand_as(emb)
                concat_emb = torch.cat([emb, lang_emb], dim=-1)
                emb = torch.relu(self.projector(concat_emb))
            else:
                raise NotImplementedError

        if context is not None:
            if self.encoder_type == "audio":
                if not self.encoder_cnn_downsampling:
                    mask_src = src.narrow(2, 0, 1).squeeze(2).eq(onmt.constants.PAD).unsqueeze(1)
                else:
                    long_mask = src.data.narrow(2, 0, 1).squeeze(2).eq(onmt.constants.PAD)
                    mask_src = long_mask[:, 0:context.size(0) * 4:4].unsqueeze(1)
            else:
                mask_src = src.eq(onmt.constants.PAD).unsqueeze(1)
        else:
            mask_src = None

        qlen = input.size(0)
        klen = input.size(0)
        mlen = klen - qlen  # extra memory if expanded
        # preparing self-attention mask. The input is either left or right aligned
        dec_attn_mask = torch.triu(
            emb.new_ones(qlen, klen), diagonal=1 + mlen).byte()[:, :, None]
        pad_mask = input.eq(onmt.constants.PAD).byte()  # L x B

        dec_attn_mask = dec_attn_mask + pad_mask.unsqueeze(0)
        dec_attn_mask = dec_attn_mask.gt(0)
        if onmt.constants.torch_version >= 1.2:
            dec_attn_mask = dec_attn_mask.bool()

        pos = torch.arange(klen - 1, -1, -1.0, device=emb.device, dtype=emb.dtype)

        pos_emb = self.positional_encoder(pos)

        output = self.preprocess_layer(emb.contiguous())

        pos_emb = self.preprocess_layer(pos_emb)

        for i, layer in enumerate(self.layer_modules):
            # batch_size x src_len x d_model
            # output, coverage = layer(output, context, pos_emb, self.r_w_bias, self.r_r_bias, dec_attn_mask, mask_src)
            output, coverage, _ = layer(output, context, pos_emb, dec_attn_mask, mask_src)

        # From Google T2T
        # if normalization is done in layer_preprocess, then it should also be done
        # on the output, since the output can grow very large, being the sum of
        # a whole stack of unnormalized layer outputs.
        output = self.postprocess_layer(output)

        output_dict = {'hidden': output, 'coverage': coverage, 'context': context}

        return output_dict

    def step(self, input, decoder_state, streaming=False):
        """
        Inputs Shapes:
            input: (Variable) batch_size x len_tgt (wanna tranpose)
            context: (Variable) batch_size x src_len x d_model
            mask_src (Tensor) batch_size x src_len
            buffer (List of tensors) List of batch_size * len_tgt-1 * d_model for self-attention recomputing
        Outputs Shapes:
            out: batch_size x len_tgt x d_model
            coverage: batch_size x len_tgt x src_len

        """

        if streaming:
            return self.step_streaming(input, decoder_state)

        context = decoder_state.context
        buffers = decoder_state.attention_buffers
        lang = decoder_state.tgt_lang
        mask_src = decoder_state.src_mask

        if decoder_state.concat_input_seq:
            if decoder_state.input_seq is None:
                decoder_state.input_seq = input
            else:
                # concatenate the last input to the previous input sequence
                decoder_state.input_seq = torch.cat([decoder_state.input_seq, input], 0)
            input = decoder_state.input_seq.transpose(0, 1)  # B x T

        src = decoder_state.src.transpose(0, 1) if decoder_state.src is not None else None

        # use the last value of input to continue decoding
        if input.size(1) > 1:
            input_ = input[:, -1].unsqueeze(1).transpose(0, 1)
        else:
            input_ = input.transpose(0, 1)
        """ Embedding: batch_size x 1 x d_model """
        emb = self.word_lut(input_) * math.sqrt(self.model_size)
        input = input.transpose(0, 1)
        klen = input.size(0)
        # emb = self.word_lut(input) * math.sqrt(self.model_size)

        if self.double_position:
            input_pos = torch.arange(input.size(0), dtype=emb.dtype, device=emb.device)
            input_pos = input_pos.unsqueeze(1).repeat(1, input.size(1))
            tgt_len, bsz = input_pos.size(0), input_pos.size(1)
            input_pos_ = input_pos.view(-1).type_as(emb)
            abs_pos = self.positional_encoder(input_pos_).squeeze(1).view(tgt_len, bsz, -1)
            emb = emb + abs_pos[-1:, :, :]

        if self.use_language_embedding:
            lang_emb = self.language_embeddings(lang)  # B x H

            if self.language_embedding_type in ['sum', 'all_sum']:
                emb = emb + lang_emb
            elif self.language_embedding_type == 'concat':
                if input.size(0) == 1:
                    emb[0] = lang_emb

                lang_emb = lang_emb.unsqueeze(0).expand_as(emb)
                concat_emb = torch.cat([emb, lang_emb], dim=-1)
                emb = torch.relu(self.projector(concat_emb))
            else:
                raise NotImplementedError

        # prepare position encoding
        qlen = emb.size(0)
        mlen = klen - qlen

        pos = torch.arange(klen - 1, -1, -1.0, device=emb.device, dtype=emb.dtype)

        pos_emb = self.positional_encoder(pos)

        dec_attn_mask = torch.triu(
            emb.new_ones(qlen, klen), diagonal=1 + mlen).byte()[:, :, None]

        pad_mask = input.eq(onmt.constants.PAD).byte()  # L x B

        dec_attn_mask = dec_attn_mask + pad_mask.unsqueeze(0)
        dec_attn_mask = dec_attn_mask.gt(0)

        if onmt.constants.torch_version >= 1.2:
            dec_attn_mask = dec_attn_mask.bool()

        if context is not None:
            if self.encoder_type == "audio":
                if not self.encoder_cnn_downsampling:
                    mask_src = src.narrow(2, 0, 1).squeeze(2).eq(onmt.constants.PAD).unsqueeze(1)
                else:
                    long_mask = src.data.narrow(2, 0, 1).squeeze(2).eq(onmt.constants.PAD)
                    mask_src = long_mask[:, 0:context.size(0) * 4:4].unsqueeze(1)
            else:

                mask_src = src.eq(onmt.constants.PAD).unsqueeze(1)
        else:
            mask_src = None

        output = emb.contiguous()

        for i, layer in enumerate(self.layer_modules):
            buffer = buffers[i] if i in buffers else None
            # assert (output.size(0) == 1)

            # output, coverage, buffer = layer.step(output, context, pos_emb,
            #                                       dec_attn_mask, mask_src, buffer=buffer)
            output, coverage, buffer = layer(output, context, pos_emb, dec_attn_mask, mask_src,
                                             incremental=True, incremental_cache=buffer)

            decoder_state.update_attention_buffer(buffer, i)

        output = self.postprocess_layer(output)
        # print(output.size())
        output = output[-1].unsqueeze(0)

        output_dict = defaultdict(lambda: None)
        output_dict['hidden'] = output
        output_dict['coverage'] = coverage
        output_dict['context'] = context

        return output_dict

    def step_streaming(self, input, decoder_state):

        context = decoder_state.context
        lang = decoder_state.tgt_lang
        streaming_state = decoder_state.streaming_state

        # for global model: push the context in

        if decoder_state.concat_input_seq:
            if decoder_state.input_seq is None:
                decoder_state.input_seq = input
            else:
                # concatenate the last input to the previous input sequence
                decoder_state.input_seq = torch.cat([decoder_state.input_seq, input], 0)
            input = decoder_state.input_seq.transpose(0, 1)  # B x T

        src = decoder_state.src.transpose(0, 1) if decoder_state.src is not None else None

        # use the last value of input to continue decoding
        if input.size(1) > 1:
            input_ = input[:, -1].unsqueeze(1).transpose(0, 1)
        else:
            input_ = input.transpose(0, 1)

        emb = self.word_lut(input_) * math.sqrt(self.model_size)
        input = input.transpose(0, 1)  # B x T to T x B
        klen = input.size(0)

        if klen == 1:
            streaming_state.reset_context_memory()

        if self.use_language_embedding:
            lang_emb = self.language_embeddings(lang)  # B x H or 1 x H
            if self.language_embedding_type == 'sum':
                emb = emb + lang_emb
            elif self.language_embedding_type == 'concat':
                # replace the bos embedding with the language
                bos_emb = lang_emb.expand_as(emb[0])
                emb[0] = bos_emb

                lang_emb = lang_emb.unsqueeze(0).expand_as(emb)
                concat_emb = torch.cat([emb, lang_emb], dim=-1)
                emb = torch.relu(self.projector(concat_emb))
            else:
                raise NotImplementedError

        # need to manually definte src_lengths and tgt_lengths here
        src_lengths = torch.LongTensor([context.size(0)])
        tgt_lengths = torch.LongTensor([1])

        if context is not None:
            context_attn_mask = self.create_context_mask(input, src, src_lengths, tgt_lengths)
            context_attn_mask = context_attn_mask.unsqueeze(0)
        else:
            context_attn_mask = None

        dec_attn_mask = self.create_self_attn_mask(input, tgt_lengths, streaming_state.prev_tgt_mem_size)

        dec_attn_mask = dec_attn_mask[:, -1:, :]

        klen = 1 + streaming_state.prev_tgt_mem_size
        pos = torch.arange(klen - 1, -1, -1.0, device=emb.device, dtype=emb.dtype)

        pos_emb = self.positional_encoder(pos)

        output = emb

        for i, layer in enumerate(self.layer_modules):
            # T x B x d_model
            buffer = streaming_state.tgt_buffer[i]
            # output, coverage = layer(output, context, pos_emb, self.r_w_bias, self.r_r_bias, dec_attn_mask, mask_src)
            # reuse_source = True if klen == 1 else False
            reuse_source = True

            # reuse source is True in this case because we can reuse the context ...
            output, coverage, buffer = layer(output, context, pos_emb, dec_attn_mask, context_attn_mask,
                                             incremental=True, incremental_cache=buffer, reuse_source=reuse_source)
            streaming_state.tgt_buffer[i] = buffer

        output = self.postprocess_layer(output)

        streaming_state.prev_tgt_mem_size += 1
        streaming_state.prune_target_memory(self.max_memory_size + input.size(0))

        output_dict = defaultdict(lambda: None, {'hidden': output, 'coverage': coverage, 'context': context})
        output_dict['streaming_state'] = streaming_state

        return output_dict


class RelativeTransformer(Transformer):

    def create_decoder_state(self, batch, beam_size=1, type=1, streaming=False, previous_decoding_state=None):
        """
        Generate a new decoder state based on the batch input
        :param previous_decoding_state:
        :param streaming:
        :param type:
        :param batch: Batch object (may not contain target during decoding)
        :param beam_size: Size of beam used in beam search
        :return:
        """

        # in this case batch size should be 1
        src = batch.get('source')
        src_pos = batch.get('source_pos')
        src_lang = batch.get('source_lang')
        tgt_lang = batch.get('target_lang')
        src_lengths = batch.src_lengths

        src_transposed = src.transpose(0, 1)

        if previous_decoding_state is None:

            # if the previous stream is None (the first segment in the stream)
            # then proceed normally like normal translation
            # init a new stream state
            streaming_state = self.init_stream()

            encoder_output = self.encoder(src_transposed, input_pos=src_pos,
                                          input_lang=src_lang, src_lengths=src_lengths,
                                          streaming=streaming, streaming_state=streaming_state)

            if streaming:

                decoder_state = StreamDecodingState(src, tgt_lang, encoder_output['context'],
                                                    encoder_output['src_mask'],
                                                    beam_size=beam_size, model_size=self.model_size, type=type,
                                                    cloning=True, streaming_state=streaming_state)
            else:
                decoder_state = TransformerDecodingState(src, tgt_lang, encoder_output['context'],
                                                         encoder_output['src_mask'],
                                                         beam_size=beam_size, model_size=self.model_size, type=type)
        else:
            streaming_state = previous_decoding_state.streaming_state

            # to have the same batch/beam size with the previous memory ..
            src_transposed = src_transposed.repeat(beam_size, 1)

            encoder_output = self.encoder(src_transposed, input_pos=src_pos,
                                          input_lang=src_lang, src_lengths=src_lengths,
                                          streaming=True, streaming_state=streaming_state)

            context = encoder_output['context']
            #
            # if self.decoder.stream_context == 'global':
            #     context = streaming_state.push_context(context, self.decoder.max_memory_size)

            decoder_state = StreamDecodingState(src, tgt_lang, context,
                                                encoder_output['src_mask'],
                                                beam_size=beam_size, model_size=self.model_size, type=type,
                                                cloning=False, streaming_state=streaming_state)
        # else:
        #     encoder_output = self.encoder(src_transposed, input_pos=src_pos, input_lang=src_lang)
        #     decoder_state = TransformerDecodingState(src, tgt_lang, encoder_output['context'],
        #                                              encoder_output['src_mask'],
        #                                              beam_size=beam_size, model_size=self.model_size, type=type)

        return decoder_state

    def init_stream(self):

        streaming_state = StreamState()
        return streaming_state

    def step(self, input_t, decoder_state, streaming=False):
        """
        Decoding function:
        generate new decoder output based on the current input and current decoder state
        the decoder state is updated in the process
        :param streaming:
        :param input_t: the input word index at time t
        :param decoder_state: object DecoderState containing the buffers required for decoding
        :return: a dictionary containing: log-prob output and the attention coverage
        """

        output_dict = self.decoder.step(input_t, decoder_state, streaming=streaming)
        output_dict['src'] = decoder_state.src.transpose(0, 1)

        # squeeze to remove the time step dimension
        log_prob = self.generator[0](output_dict).squeeze(0)

        coverage = output_dict['coverage']
        last_coverage = coverage[:, -1, :].squeeze(1)

        # output_dict = defaultdict(lambda: None)

        output_dict['log_prob'] = log_prob
        output_dict['coverage'] = last_coverage

        return output_dict

    def set_memory_size(self, src_memory_size, tgt_memory_size):

        self.encoder.max_memory_size = src_memory_size
        self.decoder.max_memory_size = tgt_memory_size


class StreamState(object):

    def __init__(self):
        self.src_buffer = defaultdict(lambda: None)
        self.prev_src_mem_size = 0
        self.src_lengths = []
        self.tgt_buffer = defaultdict(lambda: None)
        self.prev_tgt_mem_size = 0
        self.tgt_lengths = []

        self.context_memory = None

    def push_context(self, context, max_size=512):

        if self.context_memory is None:
            self.context_memory = context

        else:
            self.context_memory = torch.cat([self.context_memory, context], dim=0)

        # prune the context memory
        self.context_memory = self.context_memory[-max_size:, :, :]

        return self.context_memory

    def prune_source_memory(self, mem_size):

        pruning = mem_size < self.prev_src_mem_size
        self.prev_src_mem_size = min(mem_size, self.prev_src_mem_size)

        if pruning:
            for i in self.src_buffer:
                if self.src_buffer[i] is not None:
                    for key in self.src_buffer[i]:
                        self.src_buffer[i][key] = self.src_buffer[i][key][-mem_size:]

    def prune_target_memory(self, mem_size):

        pruning = mem_size < self.prev_tgt_mem_size
        self.prev_tgt_mem_size = min(mem_size, self.prev_tgt_mem_size)

        if pruning:
            for i in self.tgt_buffer:
                if self.tgt_buffer[i] is not None:
                    for key in self.tgt_buffer[i]:
                        self.tgt_buffer[i][key] = self.tgt_buffer[i][key][-mem_size:]

    # When we start a sentence a new, the context key and value buffers need to be reset
    def reset_context_memory(self):
        for l in self.tgt_buffer:
            buffer_ = self.tgt_buffer[l]
            buffer_.pop('c_k', None)
            buffer_.pop('c_v', None)


class StreamDecodingState(DecoderState):

    # We need to somehow create the state w.r.t the previous states of the encoder and decoder
    def __init__(self, src, tgt_lang, context, src_mask, beam_size=1, model_size=512,
                 cloning=True, streaming_state=None, **kwargs):

        self.beam_size = beam_size
        self.model_size = model_size
        self.src_mask = None
        # self.attention_buffers = dict()
        self.streaming_state = streaming_state

        bsz = src.size(1)  # this value should be 1 for

        if cloning:
            new_order = torch.arange(bsz).view(-1, 1).repeat(1, self.beam_size).view(-1)
            new_order = new_order.to(src.device)

            if context is not None:
                self.context = context.index_select(1, new_order)
            else:
                self.context = None

            self.src = src.index_select(1, new_order)  # because src is batch first
        else:
            self.context = context
            self.src = src

        # if src_mask is not None:
        #     self.src_mask = src_mask.index_select(0, new_order)
        # else:
        #     self.src_mask = None

        self.concat_input_seq = False
        self.tgt_lang = tgt_lang

    def update_attention_buffer(self, buffer, layer):

        self.attention_buffers[layer] = buffer  # dict of 2 keys (k, v) : T x B x H

    # For the new decoder version only
    def _reorder_incremental_state(self, reorder_state):

        if self.context is not None:
            self.context = self.context.index_select(1, reorder_state)

        if self.src_mask is not None:
            self.src_mask = self.src_mask.index_select(0, reorder_state)
        self.src = self.src.index_select(1, reorder_state)

        for l in self.streaming_state.src_buffer:
            buffer_ = self.streaming_state.src_buffer[l]
            if buffer_ is not None:
                for k in buffer_.keys():
                    if buffer_[k] is not None:
                        t_, br_, d_ = buffer_[k].size()
                        buffer_[k] = buffer_[k].index_select(1, reorder_state)  # 1 for time first

        for l in self.streaming_state.tgt_buffer:
            buffer_ = self.streaming_state.tgt_buffer[l]
            if buffer_ is not None:
                for k in buffer_.keys():
                    if buffer_[k] is not None:
                        t_, br_, d_ = buffer_[k].size()
                        buffer_[k] = buffer_[k].index_select(1, reorder_state)  # 1 for time first

        if self.streaming_state.context_memory is not None:
            self.streaming_state.context_memory = self.streaming_state.context_memory.index_select(1, reorder_state)

        # for l in self.attention_buffers:
        #     buffer_ = self.attention_buffers[l]
        #     if buffer_ is not None:
        #         for k in buffer_.keys():
        #             t_, br_, d_ = buffer_[k].size()
        #             buffer_[k] = buffer_[k].index_select(1, reorder_state)  # 1 for time first

    def prune_complete_beam(self, active_idx, remaining_sents):
        pass

    def update_beam(self, beam, b, remaining_sents, idx):
        pass
