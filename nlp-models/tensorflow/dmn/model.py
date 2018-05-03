from config import args
from attn_gru_cell import AttentionGRUCell

import tensorflow as tf
import numpy as np


def model_fn(features, labels, mode, params):
    if labels is None:
        labels = tf.zeros([tf.shape(features['inputs'])[0], params['max_answer_len']], tf.int64)

    logits = forward(features, params,
        reuse=False, is_training=True, seq_inputs=shift_right(labels, params))
        
    predicted_ids = forward(features, params,
        reuse=True, is_training=False, seq_inputs=None)

    loss_op = tf.reduce_mean(tf.contrib.seq2seq.sequence_loss(
        logits=logits, targets=labels, weights=tf.ones_like(labels, tf.float32)))

    if mode == tf.estimator.ModeKeys.PREDICT:
        return tf.estimator.EstimatorSpec(mode=mode, predictions=predicted_ids)
    
    if mode == tf.estimator.ModeKeys.TRAIN:
        variables = tf.trainable_variables()
        grads = tf.gradients(loss_op, variables)
        clipped_grads, _ = tf.clip_by_global_norm(grads, args.clip_norm)
        train_op = tf.train.AdamOptimizer().apply_gradients(zip(clipped_grads, variables),
            global_step=tf.train.get_global_step())
        return tf.estimator.EstimatorSpec(mode=mode, loss=loss_op, train_op=train_op)


def forward(features, params, reuse, is_training, seq_inputs=None):
    with tf.variable_scope('lookup_table', reuse=reuse):
        embedding = tf.get_variable('lookup_table', [params['vocab_size'], args.embed_dim], tf.float32)
        embedding = zero_index_pad(embedding)

    with tf.variable_scope('input_module', reuse=reuse):
        fact_vecs = input_module(features, params, embedding, reuse, is_training)
    with tf.variable_scope('question_module', reuse=reuse):
        q_vec = question_module(features, embedding, reuse)
    memory = memory_module(features, fact_vecs, q_vec, reuse, is_training)
    with tf.variable_scope('answer_module', reuse=reuse):
        logits = answer_module(features, params, memory, q_vec, embedding, reuse, is_training, seq_inputs)
    return logits


def input_module(features, params, embedding, reuse, is_training):
        inputs = tf.nn.embedding_lookup(embedding, features['inputs'])         # (B, I, S, D)
        position = position_encoding(params['max_sent_len'], args.embed_dim)
        inputs = tf.reduce_sum(inputs * position, 2)                           # (B, I, D)
        birnn_out, _ = tf.nn.bidirectional_dynamic_rnn(                                             
            GRU(args.hidden_size//2), GRU(args.hidden_size//2),
            inputs, features['inputs_len'], dtype=np.float32)
                        
        fact_vecs = tf.concat(birnn_out, -1)                                   # (B, I, D)
        fact_vecs = tf.layers.dropout(fact_vecs, args.dropout_rate, training=is_training)
        return fact_vecs


def question_module(features, embedding, reuse):
        questions = tf.nn.embedding_lookup(embedding, features['questions'])
        _, q_vec = tf.nn.dynamic_rnn(
            GRU(), questions, features['questions_len'], dtype=np.float32)
        return q_vec


def memory_module(features, fact_vecs, q_vec, reuse, is_training):
    memory = q_vec
    for i in range(args.n_hops):
        print('==> Memory Episode', i)
        episode = gen_episode(features, memory, q_vec, fact_vecs, i, reuse, is_training)
        with tf.variable_scope('memory_%d'%i, reuse=reuse):
            memory = tf.layers.dense(
                tf.concat([memory, episode, q_vec], 1), args.hidden_size, tf.nn.relu)
    return memory  # (B, D)


def gen_episode(features, memory, q_vec, fact_vecs, i, reuse, is_training):
    def gen_attn(fact_vec, _reuse=tf.AUTO_REUSE):
        with tf.variable_scope('attention', reuse=_reuse):
            features = [fact_vec * q_vec,
                        fact_vec * memory,
                        tf.abs(fact_vec - q_vec),
                        tf.abs(fact_vec - memory)]
            feature_vec = tf.concat(features, 1)
            attention = tf.layers.dense(feature_vec, args.embed_dim, tf.tanh, reuse=_reuse, name='fc1')
            attention = tf.layers.dense(attention, 1, reuse=_reuse, name='fc2')
        return tf.squeeze(attention, 1)

    # Gates (attentions) are activated, if sentence relevant to the question or memory
    attns = tf.map_fn(gen_attn, tf.transpose(fact_vecs, [1,0,2]))
    attns = tf.transpose(attns)                                    # (B, n_fact)
    attns = tf.nn.softmax(attns)                                   # (B, n_fact)
    attns = tf.expand_dims(attns, -1)                              # (B, n_fact, 1)
    
    # The relevant facts are summarized in another GRU
    reuse = (i > 0) or (not is_training)
    with tf.variable_scope('attention_gru', reuse=reuse):
        _, episode = tf.nn.dynamic_rnn(
            AttentionGRUCell(args.hidden_size, reuse=reuse),
            tf.concat([fact_vecs, attns], 2),                      # (B, n_fact, D+1)
            features['inputs_len'],
            dtype=np.float32)
    return episode                                                 # (B, D)


def answer_module(features, params, memory, q_vec, embedding, reuse, is_training, seq_inputs=None):
    memory = tf.layers.dropout(memory, args.dropout_rate, training=is_training)
    init_state = tf.layers.dense(tf.concat((memory, q_vec), -1), args.hidden_size)
    
    if is_training:
        with tf.variable_scope('decode', reuse=reuse):
            helper = tf.contrib.seq2seq.TrainingHelper(
                inputs = tf.nn.embedding_lookup(embedding, seq_inputs),
                sequence_length = tf.to_int32(features['answers_len']))
            decoder = tf.contrib.seq2seq.BasicDecoder(
                cell = GRU(),
                helper = helper,
                initial_state = init_state,
                output_layer = tf.layers.Dense(params['vocab_size']))
            decoder_output, _, _ = tf.contrib.seq2seq.dynamic_decode(
                decoder = decoder)
        return decoder_output.rnn_output
    else:
        with tf.variable_scope('decode', reuse=True):
            helper = tf.contrib.seq2seq.GreedyEmbeddingHelper(
                embedding = embedding,
                start_tokens = tf.tile(
                    tf.constant([params['<start>']], dtype=tf.int32), [tf.shape(init_state)[0]]),
                end_token = params['<end>'])
            decoder = tf.contrib.seq2seq.BasicDecoder(
                cell = GRU(reuse=True),
                helper = helper,
                initial_state = init_state,
                output_layer = tf.layers.Dense(params['vocab_size'], _reuse=True))
            decoder_output, _, _ = tf.contrib.seq2seq.dynamic_decode(
                decoder = decoder,
                maximum_iterations = params['max_answer_len'])
        return decoder_output.sample_id


def shift_right(x, params):
    batch_size = tf.shape(x)[0]
    start = tf.to_int64(tf.fill([batch_size, 1], params['<start>']))
    return tf.concat([start, x[:, :-1]], 1)


def GRU(rnn_size=None, reuse=None):
    rnn_size = args.hidden_size if rnn_size is None else rnn_size
    return tf.nn.rnn_cell.GRUCell(
        rnn_size, kernel_initializer=tf.orthogonal_initializer(), reuse=reuse)


def zero_index_pad(embedding):
    return tf.concat((tf.zeros([1, args.embed_dim]), embedding[1:, :]), axis=0)


def position_encoding(sentence_size, embedding_size):
    encoding = np.ones((embedding_size, sentence_size), dtype=np.float32)
    ls = sentence_size + 1
    le = embedding_size + 1
    for i in range(1, le):
        for j in range(1, ls):
            encoding[i-1, j-1] = (i - (le-1)/2) * (j - (ls-1)/2)
    encoding = 1 + 4 * encoding / embedding_size / sentence_size
    return np.transpose(encoding)
