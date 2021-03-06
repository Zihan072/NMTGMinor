import torch
import torch.nn as nn
from onmt.models.transformer_layers import PositionalEncoding, PrePostProcessing
from onmt.models.transformer_layers import EncoderLayer, DecoderLayer
from onmt.models.transformers import TransformerEncoder, TransformerDecoder, TransformerDecodingState
import onmt
from onmt.modules.dropout import embedded_dropout
from onmt.models.transformer_layers import XavierLinear, MultiHeadAttention, FeedForward, PrePostProcessing
from onmt.models.transformer_layers import EncoderLayer, DecoderLayer
from onmt.models.relative_transformer_layers import RelativeTransformerEncoderLayer, RelativeTransformerDecoderLayer
from onmt.models.unified_transformer import UnifiedTransformer
from onmt.models.relative_transformer import SinusoidalPositionalEmbedding, LearnablePostionEmbedding
from onmt.utils import flip, expected_length
from collections import defaultdict
import math

torch.set_printoptions(profile="full")


class RelativeUnifiedTransformer(UnifiedTransformer):
    """
    This class combines the encoder and the decoder into one single sequence
    Joined attention between encoder and decoder parts
    """

    def __init__(self, opt, src_embedding, tgt_embedding, generator, positional_encoder,
                 language_embeddings=None, encoder_type='text', **kwargs):
        self.death_rate = opt.death_rate
        self.bidirectional = opt.bidirectional
        self.layer_modules = []
        self.learnable_position_encoding = opt.learnable_position_encoding

        # build_modules will be called from the inherited constructor
        super(RelativeUnifiedTransformer, self).__init__(opt, tgt_embedding, src_embedding,
                                                         generator, positional_encoder,
                                                         language_embeddings=language_embeddings,
                                                         encoder_type=encoder_type)
        self.src_embedding = src_embedding
        self.tgt_embedding = tgt_embedding
        # self.language_embedding = nn.Embedding(3, self.model_size, padding_idx=0)
        self.generator = generator
        self.ignore_source = True
        self.encoder_type = opt.encoder_type

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
        # self.positional_encoder = SinusoidalPositionalEmbedding(opt.model_size)
        self.d_head = self.model_size // self.n_heads

        # self.build_modules()

    def gen_mask(self, src, tgt):

        # both src and tgt are T x B
        input_seq = torch.cat([src, tgt], dim=0)
        seq_len = input_seq.size(0)

        if self.bidirectional:
            bsz, src_len = src.size(1), src.size(0)
            tgt_len = tgt.size(0)

            tgt_tgt_mask = torch.triu(src.new_ones(tgt_len, tgt_len), diagonal=1)
            tgt_src_mask = src.new_zeros(tgt_len, src_len)

            tgt_mask = torch.cat([tgt_src_mask, tgt_tgt_mask], dim=-1)

            src_src_mask = src.new_zeros(src_len, src_len)
            src_tgt_mask = src.new_ones(src_len, tgt_len)

            src_mask = torch.cat([src_src_mask, src_tgt_mask], dim=-1)

            attn_mask = torch.cat([src_mask, tgt_mask], dim=0)

            attn_mask = attn_mask.bool().unsqueeze(-1)

            pad_mask = input_seq.eq(onmt.constants.PAD).unsqueeze(0)

            attn_mask = attn_mask | pad_mask

        else:
            attn_mask = torch.triu(src.new_ones(seq_len, seq_len), diagonal=1).bool().unsqueeze(-1)  # T x T x -1

            pad_mask = input_seq.eq(onmt.constants.PAD).unsqueeze(0)  # 1 x T x B
            # attn_mask = self.mask[:seq_len, :seq_len] + input_seq.eq(onmt.constants.PAD).byte().unsqueeze(1)
            attn_mask = attn_mask | pad_mask

        return attn_mask

    def build_modules(self):

        e_length = expected_length(self.layers, self.death_rate)
        print("* Transformer Decoder with Relative Attention with %.2f expected layers" % e_length)

        self.layer_modules = nn.ModuleList()

        for l in range(self.layers):
            # linearly decay the death rate
            death_r = (l + 1.0) / self.layers * self.death_rate

            block = RelativeTransformerDecoderLayer(self.n_heads, self.model_size,
                                                    self.dropout, self.inner_size, self.attn_dropout,
                                                    ignore_source=True,
                                                    variational=self.variational_dropout, death_rate=death_r)
            self.layer_modules.append(block)

    def forward(self, batch, target_mask=None, **kwargs):

        src = batch.get('source')  # src_len x batch_size
        tgt = batch.get('target_input')  # len_tgt x batch_size
        src_pos = batch.get('source_pos')
        tgt_pos = batch.get('target_pos')
        src_lang = batch.get('source_lang')
        tgt_lang = batch.get('target_lang')

        tgt_len = tgt.size(0)
        src_len = src.size(0)
        bsz = tgt.size(1)

        # Embedding stage (and scale the embedding)
        src_emb = embedded_dropout(self.src_embedding, src, dropout=self.word_dropout if self.training else 0) \
                  * math.sqrt(self.model_size)
        tgt_emb = embedded_dropout(self.tgt_embedding, tgt, dropout=self.word_dropout if self.training else 0) \
                  * math.sqrt(self.model_size)

        if self.use_language_embedding:
            if self.language_embedding_type in ["sum", "all_sum"]:
                src_lang_emb = self.language_embeddings(src_lang)
                src_emb += src_lang_emb
                tgt_lang_emb = self.language_embeddings(tgt_lang)
                tgt_emb += tgt_lang_emb
            else:
                raise NotImplementedError

        # concatenate embedding
        emb = torch.cat([src_emb, tgt_emb], dim=0)  # L x batch_size x H

        # prepare self-attention mask
        attn_mask = self.gen_mask(src, tgt)

        # pos = torch.arange(klen - 1, -1, -1.0, device=emb.device, dtype=emb.dtype)
        klen = src_len + tgt_len
        pos = torch.arange(klen - 1, -klen, -1.0, device=emb.device, dtype=emb.dtype)

        pos_emb = self.positional_encoder(pos)

        output = emb

        # Applying dropout
        output = self.preprocess_layer(output)

        # FORWARD PASS
        coverage = None
        for i, layer in enumerate(self.layer_modules):
            output, coverage, _ = layer(output, None, pos_emb, attn_mask, None)  # context and context_mask are None

        # Final normalization
        output = self.postprocess_layer(output)

        # extract the "source" and "target" parts of the output
        context = output[:src_len, :, :]
        output = output[-tgt_len:, :, :]
        output_dict = {'hidden': output, 'coverage': coverage, 'context': context, 'src': src,
                       'target_mask': target_mask}

        # final layer: computing log probabilities
        logprobs = self.generator[0](output_dict)
        output_dict['logprobs'] = logprobs

        return output_dict

    def encode(self, input, decoder_state, input_pos=None, input_lang=None):

        buffers = decoder_state.attention_buffers
        src_lang = input_lang
        input = input.transpose(0, 1)
        # Embedding stage (and scale the embedding)
        src_emb = embedded_dropout(self.src_embedding, input, dropout=self.word_dropout if self.training else 0) \
                  * math.sqrt(self.model_size)

        if self.use_language_embedding:
            if self.language_embedding_type in ["sum", "all_sum"]:
                src_lang_emb = self.language_embeddings(src_lang)
                src_emb += src_lang_emb

        emb = src_emb
        src_len = input.size(0)
        bsz = input.size(1)
        mask_src_src = input.eq(onmt.constants.PAD).byte()  # B x 1 x src_len
        mask_src = mask_src_src.unsqueeze(0)

        attn_mask = mask_src.bool()  # L x L x batch_size

        output = emb

        # Applying dropout and tranpose to T x B x H
        output = self.preprocess_layer(output)

        klen = src_len
        pos = torch.arange(klen - 1, -klen, -1.0, device=emb.device, dtype=emb.dtype)

        pos_emb = self.positional_encoder(pos)

        # FORWARD PASS
        coverage = None
        for i, layer in enumerate(self.layer_modules):
            # context and context_mask are None
            buffer = buffers[i] if i in buffers else None
            # output, coverage, buffer = layer.step(output, None, attn_mask, None, buffer)
            output, coverage, buffer = layer(output, None, pos_emb, attn_mask, None,
                                             incremental=True, incremental_cache=buffer)
            decoder_state.update_attention_buffer(buffer, i)

        # Final normalization
        output = self.postprocess_layer(output)

        return output, decoder_state

    def decode(self, batch):
        """
        :param batch: (onmt.Dataset.Batch) an object containing tensors needed for training
        :return: gold_scores (torch.Tensor) log probs for each sentence
                 gold_words  (Int) the total number of non-padded tokens
                 allgold_scores (list of Tensors) log probs for each word in the sentence
        """
        # raise NotImplementedError
        tgt_output = batch.get('target_output')
        output_dict = self.forward(batch, target_mask=None)
        context = output_dict['context']
        logprobs = output_dict['logprobs']

        batch_size = logprobs.size(1)

        gold_scores = context.new(batch_size).zero_()
        gold_words = 0
        allgold_scores = list()

        for gen_t, tgt_t in zip(logprobs, tgt_output):
            tgt_t = tgt_t.unsqueeze(1)
            scores = gen_t.gather(1, tgt_t)
            scores.masked_fill_(tgt_t.eq(onmt.constants.PAD), 0)
            gold_scores += scores.squeeze(1).type_as(gold_scores)
            gold_words += tgt_t.ne(onmt.constants.PAD).sum().item()
            allgold_scores.append(scores.squeeze(1).type_as(gold_scores))

        return gold_words, gold_scores, allgold_scores

    def renew_buffer(self, new_len):

        # This model uses pre-allocated position encoding
        self.positional_encoder.renew(new_len)
        mask = torch.ByteTensor(np.triu(np.ones((new_len + 1, new_len + 1)), k=1).astype('uint8'))
        self.register_buffer('mask', mask)

        return

    def reset_states(self):
        return

    def step(self, input, decoder_state):

        src = decoder_state.src if decoder_state.src is not None else None
        tgt = input.transpose(0, 1)
        tgt_lang = decoder_state.tgt_lang
        src_lang = decoder_state.src_lang
        buffers = decoder_state.attention_buffers

        tgt_len = tgt.size(0)
        src_len = src.size(0)
        bsz = tgt.size(1)

        # Embedding stage (and scale the embedding)
        # src_emb = embedded_dropout(self.src_embedding, src, dropout=self.word_dropout if self.training else 0) \
        #           * math.sqrt(self.model_size)
        input_ = tgt[-1:]
        tgt_emb = embedded_dropout(self.tgt_embedding, input_, dropout=self.word_dropout if self.training else 0) \
                  * math.sqrt(self.model_size)

        if self.use_language_embedding:
            if self.language_embedding_type in ["sum", "all_sum"]:
                # src_lang_emb = self.language_embeddings(src_lang)
                # src_emb += src_lang_emb
                tgt_lang_emb = self.language_embeddings(tgt_lang)
                tgt_emb += tgt_lang_emb
            else:
                raise NotImplementedError

        # concatenate embedding
        # emb = torch.cat([src_emb, tgt_emb], dim=0)  # L x batch_size x H
        emb = tgt_emb

        # prepare self-attention mask

        attn_mask = self.gen_mask(src, tgt)
        # last attn_mask step
        attn_mask = attn_mask[-1:, :, :]

        klen = src_len + tgt_len
        pos = torch.arange(klen - 1, -1, -1.0, device=emb.device, dtype=emb.dtype)

        pos_emb = self.positional_encoder(pos)

        output = emb

        # Applying dropout
        output = self.preprocess_layer(output)

        # FORWARD PASS
        coverage = None
        for i, layer in enumerate(self.layer_modules):
            buffer = buffers[i] if i in buffers else None
            output, coverage, buffer = layer(output, None, pos_emb, attn_mask, None,
                                             incremental=True,
                                             incremental_cache=buffer)  # context and context_mask are None
            decoder_state.update_attention_buffer(buffer, i)

        # Final normalization
        output = self.postprocess_layer(output)

        # output = output[-1:, :, :]

        output_dict = defaultdict(lambda: None)
        output_dict['hidden'] = output

        logprobs = self.generator[0](output_dict).squeeze(0)

        output_dict['src'] = decoder_state.src.transpose(0, 1)
        output_dict['log_prob'] = logprobs
        output_dict['coverage'] = logprobs.new(bsz, tgt_len, src_len).zero_()

        return output_dict

    def create_decoder_state(self, batch, beam_size=1, type=1):

        src = batch.get('source')
        src_pos = batch.get('source_pos')
        src_lang = batch.get('source_lang')
        tgt_lang = batch.get('target_lang')

        src_transposed = src.transpose(0, 1)  # B x T

        decoder_state = TransformerDecodingState(src, tgt_lang, None, None,
                                                 beam_size=beam_size, model_size=self.model_size, type=type)

        # forward pass through the input to get the buffer
        # src_transposed = src_transposed.repeat(beam_size, 1)
        encoder_output, decoder_state = self.encode(src_transposed, decoder_state, input_pos=src_pos, input_lang=src_lang)

        decoder_state.src_lang = src_lang

        buffers = decoder_state.attention_buffers
        bsz = src.size(1)
        new_order = torch.arange(bsz).view(-1, 1).repeat(1, beam_size).view(-1)
        new_order = new_order.to(src.device)

        for l in buffers:
            buffer_ = buffers[l]
            if buffer_ is not None:
                for k in buffer_.keys():
                    t_, br_, d_ = buffer_[k].size()
                    buffer_[k] = buffer_[k].index_select(1, new_order)  # 1 for time first

        return decoder_state

    def tie_weights(self):
        assert self.generator is not None, "The generator needs to be created before sharing weights"
        self.generator[0].linear.weight = self.tgt_embedding.weight

    def share_enc_dec_embedding(self):
        self.src_embedding.weight = self.tgt_embedding.weight
