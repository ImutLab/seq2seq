from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import functools
import math
from tensorflow.python.ops import rnn_cell, rnn
from translate.rnn import multi_rnn, multi_bidirectional_rnn
from collections import namedtuple


def unsafe_decorator(fun):
  """
  Wrapper that automatically handles the `reuse' parameter.
  This is rather unsafe, as it can lead to reusing variables
  by mistake, without knowing about it.
  """
  def fun_(*args, **kwargs):
    try:
      return fun(*args, **kwargs)
    except ValueError as e:
      if 'reuse' in str(e):
        with tf.variable_scope(tf.get_variable_scope(), reuse=True):
          return fun(*args, **kwargs)
      else:
        raise e
  return fun_


get_variable_unsafe = unsafe_decorator(tf.get_variable)
GRUCell_unsafe = unsafe_decorator(rnn_cell.GRUCell)
BasicLSTMCell_unsafe = unsafe_decorator(rnn_cell.BasicLSTMCell)
MultiRNNCell_unsafe = unsafe_decorator(rnn_cell.MultiRNNCell)
linear_unsafe = unsafe_decorator(rnn_cell._linear)
multi_rnn_unsafe = unsafe_decorator(multi_rnn)
multi_bidirectional_rnn_unsafe = unsafe_decorator(multi_bidirectional_rnn)


def multi_encoder(encoder_inputs, encoders, encoder_input_length=None, dropout=None, residual_connections=False,
                  **kwargs):
  assert len(encoder_inputs) == len(encoders)
  encoder_states = []
  encoder_outputs = []

  # create embeddings in the global scope (allows sharing between encoder and decoder)
  embedding_variables = []
  for encoder in encoders:
    # inputs are token ids, which need to be mapped to vectors (embeddings)
    if not encoder.binary:
      if encoder.get('embedding') is not None:
        initializer = encoder.embedding
        embedding_shape = None
      else:
        initializer = tf.random_uniform_initializer(-math.sqrt(3), math.sqrt(3))
        embedding_shape = [encoder.vocab_size, encoder.embedding_size]

      # TODO: shared name
      with tf.device('/cpu:0'):
        embedding = get_variable_unsafe('embedding_{}'.format(encoder.name), shape=embedding_shape,
                                        initializer=initializer)
      embedding_variables.append(embedding)
    else:  # do nothing: inputs are already vectors
      embedding_variables.append(None)

  with tf.variable_scope('multi_encoder'):
    if encoder_input_length is None:
      encoder_input_length = [None] * len(encoders)

    for i, encoder in enumerate(encoders):
      with tf.variable_scope(encoder.name):
        encoder_inputs_ = encoder_inputs[i]
        encoder_input_length_ = encoder_input_length[i]

        # TODO: use state_is_tuple=True
        if encoder.use_lstm:
          cell = rnn_cell.BasicLSTMCell(encoder.cell_size)
        else:
          cell = rnn_cell.GRUCell(encoder.cell_size)

        if dropout is not None:
          cell = rnn_cell.DropoutWrapper(cell, input_keep_prob=dropout)

        embedding = embedding_variables[i]

        if embedding is not None or encoder.input_layers:
          batch_size = tf.shape(encoder_inputs_)[0]   # TODO: fix this time major shit
          time_steps = tf.shape(encoder_inputs_)[1]

          if embedding is None:
            size = encoder_inputs_.get_shape()[2].value
            flat_inputs = tf.reshape(encoder_inputs_, [tf.mul(batch_size, time_steps), size])
          else:
            flat_inputs = tf.reshape(encoder_inputs_, [tf.mul(batch_size, time_steps)])
            flat_inputs = tf.nn.embedding_lookup(embedding, flat_inputs)

          if encoder.input_layers:
            for j, size in enumerate(encoder.input_layers):
              name = 'input_layer_{}'.format(j)
              flat_inputs = tf.nn.tanh(linear_unsafe(flat_inputs, size, bias=True, scope=name))
              if dropout is not None:
                flat_inputs = tf.nn.dropout(flat_inputs, dropout)

          encoder_inputs_ = tf.reshape(flat_inputs, tf.pack([batch_size, time_steps, flat_inputs.get_shape()[1].value]))

        # encoder_inputs_ = tf.transpose(encoder_inputs_, perm=[1, 0, 2])   # put batch_size first
        sequence_length = encoder_input_length_
        parameters = dict(
          inputs=encoder_inputs_, sequence_length=sequence_length,
          time_pooling=encoder.time_pooling, pooling_avg=encoder.pooling_avg, dtype=tf.float32,
          swap_memory=encoder.swap_memory, parallel_iterations=encoder.parallel_iterations,
          residual_connections=residual_connections
        )

        if encoder.bidir:
          encoder_outputs_, _, encoder_state_ = multi_bidirectional_rnn_unsafe(
            cells=[(cell, cell)] * encoder.layers, **parameters)
        else:
          encoder_outputs_, encoder_state_ = multi_rnn_unsafe(
            cells=[cell] * encoder.layers, **parameters)

        if encoder.bidir:  # map to correct output dimension
          # there is not tensor product operation, so we need to flatten our tensor to
          # a matrix to perform a dot product
          shape = tf.shape(encoder_outputs_)
          batch_size = shape[0]
          time_steps = shape[1]
          dim = encoder_outputs_.get_shape()[2]
          outputs_ = tf.reshape(encoder_outputs_, tf.pack([tf.mul(batch_size, time_steps), dim]))
          outputs_ = linear_unsafe(outputs_, cell.output_size, False, scope='bidir_projection')
          encoder_outputs_ = tf.reshape(outputs_, tf.pack([batch_size, time_steps, cell.output_size]))

        encoder_outputs.append(encoder_outputs_)
        encoder_states.append(encoder_state_)

    encoder_state = tf.concat(1, encoder_states)
    return encoder_outputs, encoder_state


def compute_energy(hidden, state, name, **kwargs):
  attn_size = hidden.get_shape()[3].value
  batch_size = tf.shape(hidden)[0]
  time_steps = tf.shape(hidden)[1]

  y = linear_unsafe(state, attn_size, True, scope=name)
  y = tf.reshape(y, [-1, 1, 1, attn_size])

  k = get_variable_unsafe('W_{}'.format(name), [attn_size, attn_size])

  # dot product between tensors needs reshaping
  hidden = tf.reshape(hidden, tf.pack([tf.mul(batch_size, time_steps), attn_size]))
  f = tf.matmul(hidden, k)
  f = tf.reshape(f, tf.pack([batch_size, time_steps, 1, attn_size]))

  v = get_variable_unsafe('V_{}'.format(name), [attn_size])
  s = f + y

  return tf.reduce_sum(v * tf.tanh(s), [2, 3])


def compute_energy_with_filter(hidden, state, name, prev_weights, attention_filters,
                               attention_filter_length, **kwargs):
  time_steps = tf.shape(hidden)[1]
  attn_size = hidden.get_shape()[3].value
  batch_size = tf.shape(hidden)[0]

  filter_shape = [attention_filter_length * 2 + 1, 1, 1, attention_filters]
  filter_ = get_variable_unsafe('filter_{}'.format(name), filter_shape)
  u = get_variable_unsafe('U_{}'.format(name), [attention_filters, attn_size])
  prev_weights = tf.reshape(prev_weights, tf.pack([batch_size, time_steps, 1, 1]))
  conv = tf.nn.conv2d(prev_weights, filter_, [1, 1, 1, 1], 'SAME')
  shape = tf.pack([tf.mul(batch_size, time_steps), attention_filters])
  conv = tf.reshape(conv, shape)
  z = tf.matmul(conv, u)
  z = tf.reshape(z, tf.pack([batch_size, time_steps, 1, attn_size]))

  y = linear_unsafe(state, attn_size, True)
  y = tf.reshape(y, [-1, 1, 1, attn_size])

  k = get_variable_unsafe('W_{}'.format(name), [attn_size, attn_size])

  # dot product between tensors needs reshaping
  hidden = tf.reshape(hidden, tf.pack([tf.mul(batch_size, time_steps), attn_size]))
  f = tf.matmul(hidden, k)
  f = tf.reshape(f, tf.pack([batch_size, time_steps, 1, attn_size]))

  v = get_variable_unsafe('V_{}'.format(name), [attn_size])
  s = f + y + z
  return tf.reduce_sum(v * tf.tanh(s), [2, 3])


def global_attention(state, prev_weights, hidden_states, encoder, **kwargs):
  with tf.variable_scope('attention'):

    compute_energy_ = compute_energy_with_filter if encoder.attention_filters > 0 else compute_energy
    e = compute_energy_(hidden_states, state, encoder.name,
                        prev_weights=prev_weights, attention_filters=encoder.attention_filters,
                        attention_filter_length=encoder.attention_filter_length)
    weights = tf.nn.softmax(e)

    shape = tf.shape(weights)
    shape = tf.pack([shape[0], shape[1], 1, 1])

    weighted_average = tf.reduce_sum(tf.reshape(weights, shape) * hidden_states, [1, 2])
    return weighted_average, weights


def local_attention(state, prev_weights, hidden_states, encoder, **kwargs):
  """
  Local attention of Luong et al. (http://arxiv.org/abs/1508.04025)
  """
  attn_length = tf.shape(hidden_states)[1]
  state_size = state.get_shape()[1].value

  with tf.variable_scope('attention'):
    # S = hidden_states.get_shape()[1].value   # source length
    S = tf.cast(attn_length, dtype=tf.float32)

    wp = get_variable_unsafe('Wp_{}'.format(encoder.name), [state_size, state_size])
    vp = get_variable_unsafe('vp_{}'.format(encoder.name), [state_size, 1])

    pt = tf.nn.sigmoid(tf.matmul(tf.nn.tanh(tf.matmul(state, wp)), vp))
    pt = tf.floor(S * tf.reshape(pt, [-1, 1]))  # aligned position in the source sentence

    batch_size = tf.shape(state)[0]

    idx = tf.tile(tf.cast(tf.range(attn_length), dtype=tf.float32), tf.pack([batch_size]))
    idx = tf.reshape(idx, [-1, attn_length])

    low = pt - encoder.attention_window_size
    high = pt + encoder.attention_window_size

    mlow = tf.to_float(idx < low)
    mhigh =  tf.to_float(idx > high)
    m = mlow + mhigh
    mask = tf.to_float(tf.equal(m, 0.0))

    compute_energy_ = compute_energy_with_filter if encoder.attention_filters > 0 else compute_energy
    e = compute_energy_(hidden_states, state, encoder.name,
                        prev_weights=prev_weights, attention_filters=encoder.attention_filters,
                        attention_filter_length=encoder.attention_filter_length)

    # we have to use this mask thing, because the slice operation
    # does not work with batch dependent indices
    # hopefully softmax is more efficient with sparse vectors
    weights = tf.nn.softmax(e * mask)

    sigma = encoder.attention_window_size / 2
    numerator = -tf.pow((idx - pt), tf.convert_to_tensor(2, dtype=tf.float32))
    div = tf.truediv(numerator, sigma ** 2)

    weights = weights * tf.exp(div)   # result of the truncated normal distribution
    weighted_average = tf.reduce_sum(tf.reshape(weights, [-1, attn_length, 1, 1]) * hidden_states, [1, 2])
    return weighted_average, weights


def attention(state, prev_weights, hidden_states, encoder, **kwargs):
  """
  Proxy for `local_attention` and `global_attention`
  """
  if encoder.attention_window_size > 0:
    attention_ = local_attention
  else:
    attention_ = global_attention

  return attention_(state, prev_weights, hidden_states, encoder, **kwargs)


def multi_attention(state, prev_weights, hidden_states, encoders, **kwargs):
  """
  Same as `attention` except that prev_weights, hidden_states and encoders
  are lists whose length is the number of encoders.
  """
  ds, weights = zip(*[attention(state, weights_, hidden, encoder)
                      for weights_, hidden, encoder in zip(prev_weights, hidden_states, encoders)])

  return tf.concat(1, ds), list(weights)


def decoder(decoder_inputs, initial_state, decoder, decoder_input_length=None, output_projection=None, dropout=None,
            feed_previous=0.0, **kwargs):
  if decoder.get('embedding') is not None:
    embedding_initializer = decoder.embedding
    embedding_shape = None
  else:
    embedding_initializer = None
    embedding_shape = [decoder.vocab_size, decoder.embedding_size]

  with tf.device('/cpu:0'):
    embedding = get_variable_unsafe('embedding_{}'.format(decoder.name),
                                    shape=embedding_shape,
                                    initializer=embedding_initializer)

  if decoder.use_lstm:
    cell = rnn_cell.BasicLSTMCell(decoder.cell_size)
  else:
    cell = rnn_cell.GRUCell(decoder.cell_size)

  if dropout is not None:
    cell = rnn_cell.DropoutWrapper(cell, input_keep_prob=dropout)

  if decoder.layers > 1:
    cell = rnn_cell.MultiRNNCell([cell] * decoder.layers)

  if output_projection is None:
    cell = rnn_cell.OutputProjectionWrapper(cell, decoder.vocab_size)
    output_size = decoder.vocab_size
  else:
    output_size = cell.output_size

  if output_projection is not None:
    proj_weights = tf.convert_to_tensor(output_projection[0], dtype=tf.float32)
    proj_weights.get_shape().assert_is_compatible_with([cell.output_size, decoder.vocab_size])
    proj_biases = tf.convert_to_tensor(output_projection[1], dtype=tf.float32)
    proj_biases.get_shape().assert_is_compatible_with([decoder.vocab_size])

  with tf.variable_scope('decoder_{}'.format(decoder.name)):
    def extract_argmax_and_embed(prev):
      if output_projection is not None:
        prev = tf.nn.xw_plus_b(prev, output_projection[0], output_projection[1])
      prev_symbol = tf.stop_gradient(tf.argmax(prev, 1))
      emb_prev = tf.nn.embedding_lookup(embedding, prev_symbol)
      return emb_prev

    if embedding is not None:
      time_steps = tf.shape(decoder_inputs)[0]
      batch_size = tf.shape(decoder_inputs)[1]
      flat_inputs = tf.reshape(decoder_inputs, [tf.mul(batch_size, time_steps)])
      flat_inputs = tf.nn.embedding_lookup(embedding, flat_inputs)
      decoder_inputs = tf.reshape(flat_inputs, tf.pack([time_steps, batch_size, flat_inputs.get_shape()[1].value]))

    if initial_state.get_shape()[1] == cell.state_size:
      state = initial_state
    else:
      state = linear_unsafe(initial_state, cell.state_size, False, scope='initial_state_projection')

    sequence_length = decoder_input_length
    if sequence_length is not None:
      sequence_length = tf.to_int32(sequence_length)
      min_sequence_length = tf.reduce_min(sequence_length)
      max_sequence_length = tf.reduce_max(sequence_length)

    time = tf.constant(0, dtype=tf.int32, name="time")

    input_shape = tf.shape(decoder_inputs)
    time_steps = input_shape[0]
    batch_size = input_shape[1]
    state_size = cell.state_size

    zero_output = tf.zeros(tf.pack([batch_size, cell.output_size]), tf.float32)

    state_ta = tf.TensorArray(dtype=tf.float32, size=time_steps)
    output_ta = tf.TensorArray(dtype=tf.float32, size=time_steps, clear_after_read=False)
    input_ta = tf.TensorArray(dtype=tf.float32, size=time_steps).unpack(decoder_inputs)

    def _time_step(time, state, output_ta_t, state_ta_t):
      input_t = input_ta.read(time)
      # restore some shape information
      r = tf.random_uniform([])
      input_t = tf.cond(tf.logical_and(time > 0, r < feed_previous),
                        lambda: tf.stop_gradient(extract_argmax_and_embed(output_ta_t.read(time - 1))),
                        lambda: input_t)
      input_t.set_shape(decoder_inputs.get_shape()[1:])
      x = linear_unsafe([input_t], input_t.get_shape()[1], True)
      call_cell = lambda: unsafe_decorator(cell)(x, state)

      if sequence_length is not None:
        output, new_state = rnn._rnn_step(
          time=time,
          sequence_length=sequence_length,
          min_sequence_length=min_sequence_length,
          max_sequence_length=max_sequence_length,
          zero_output=zero_output,
          state=state,
          call_cell=call_cell,
          state_size=state_size,
          skip_conditionals=True)
      else:
        output, new_state = call_cell()

      state_ta_t = state_ta_t.write(time, new_state)

      if output_projection is not None:
        with tf.variable_scope('output_projection'):
          output = linear_unsafe([output], output_size, True)

      output_ta_t = output_ta_t.write(time, output)
      return time + 1, new_state, output_ta_t, state_ta_t

    _, _, output_final_ta, state_final_ta = tf.while_loop(
      cond=lambda time, *_: time < time_steps,
      body=_time_step,
      loop_vars=(time, state, output_ta, state_ta),
      parallel_iterations=decoder.parallel_iterations,
      swap_memory=decoder.swap_memory)

    outputs = output_final_ta.pack()
    decoder_states = state_final_ta.pack()

    return outputs, decoder_states, None


def attention_decoder(decoder_inputs, initial_state, attention_states, encoders, decoder,
                      decoder_input_length=None, attention_weights=None, output_projection=None,
                      initial_state_attention=False, dropout=None,
                      feed_previous=0.0, **kwargs):
  if decoder.get('embedding') is not None:
    embedding_initializer = decoder.embedding
    embedding_shape = None
  else:
    embedding_initializer = None
    embedding_shape = [decoder.vocab_size, decoder.embedding_size]

  with tf.device('/cpu:0'):
    embedding = get_variable_unsafe('embedding_{}'.format(decoder.name),
                                    shape=embedding_shape,
                                    initializer=embedding_initializer)

  if decoder.use_lstm:
    cell = rnn_cell.BasicLSTMCell(decoder.cell_size)
  else:
    cell = rnn_cell.GRUCell(decoder.cell_size)

  if dropout is not None:
    cell = rnn_cell.DropoutWrapper(cell, input_keep_prob=dropout)

  if decoder.layers > 1:
    cell = rnn_cell.MultiRNNCell([cell] * decoder.layers)

  if output_projection is None:
    cell = rnn_cell.OutputProjectionWrapper(cell, decoder.vocab_size)
    output_size = decoder.vocab_size
  else:
    output_size = cell.output_size

  if output_projection is not None:
    proj_weights = tf.convert_to_tensor(output_projection[0], dtype=tf.float32)
    proj_weights.get_shape().assert_is_compatible_with([cell.output_size, decoder.vocab_size])
    proj_biases = tf.convert_to_tensor(output_projection[1], dtype=tf.float32)
    proj_biases.get_shape().assert_is_compatible_with([decoder.vocab_size])

  with tf.variable_scope('decoder_{}'.format(decoder.name)):
    def extract_argmax_and_embed(prev):
      if output_projection is not None:
        prev = tf.nn.xw_plus_b(prev, output_projection[0], output_projection[1])
      prev_symbol = tf.stop_gradient(tf.argmax(prev, 1))
      emb_prev = tf.nn.embedding_lookup(embedding, prev_symbol)
      return emb_prev

    if embedding is not None:
      time_steps = tf.shape(decoder_inputs)[0]
      batch_size = tf.shape(decoder_inputs)[1]
      flat_inputs = tf.reshape(decoder_inputs, [tf.mul(batch_size, time_steps)])
      flat_inputs = tf.nn.embedding_lookup(embedding, flat_inputs)
      decoder_inputs = tf.reshape(flat_inputs, tf.pack([time_steps, batch_size, flat_inputs.get_shape()[1].value]))

    attn_lengths = [tf.shape(states)[1] for states in attention_states]
    attn_size = sum(states.get_shape()[2].value for states in attention_states)

    hidden_states = [tf.expand_dims(states, 2) for states in attention_states]
    attention_ = functools.partial(multi_attention, hidden_states=hidden_states, encoders=encoders)

    if initial_state.get_shape()[1] == cell.state_size:
      state = initial_state
    else:
      state = linear_unsafe(initial_state, cell.state_size, False, scope='initial_state_projection')

    sequence_length = decoder_input_length
    if sequence_length is not None:
      sequence_length = tf.to_int32(sequence_length)
      min_sequence_length = tf.reduce_min(sequence_length)
      max_sequence_length = tf.reduce_max(sequence_length)

    time = tf.constant(0, dtype=tf.int32, name="time")

    input_shape = tf.shape(decoder_inputs)
    time_steps = input_shape[0]
    batch_size = input_shape[1]
    state_size = cell.state_size

    zero_output = tf.zeros(tf.pack([batch_size, cell.output_size]), tf.float32)

    state_ta = tf.TensorArray(dtype=tf.float32, size=time_steps)
    output_ta = tf.TensorArray(dtype=tf.float32, size=time_steps, clear_after_read=False)
    input_ta = tf.TensorArray(dtype=tf.float32, size=time_steps).unpack(decoder_inputs)
    attn_weights_ta = tf.TensorArray(dtype=tf.float32, size=time_steps)

    if attention_weights is None:
      attention_weights = [tf.zeros(tf.pack([batch_size, length])) for length in attn_lengths]

    if initial_state_attention:
      attns, attention_weights = attention_(state, prev_weights=attention_weights)
    else:
      attns = tf.zeros(tf.pack([batch_size, attn_size]), dtype=tf.float32)
      attns.set_shape([None, attn_size])

    def _time_step(time, state, attns, attn_weights, output_ta_t, state_ta_t, attn_weights_ta_t):
      input_t = input_ta.read(time)
      # restore some shape information
      r = tf.random_uniform([])
      input_t = tf.cond(tf.logical_and(time > 0, r < feed_previous),
                        lambda: tf.stop_gradient(extract_argmax_and_embed(output_ta_t.read(time - 1))),
                        lambda: input_t)
      input_t.set_shape(decoder_inputs.get_shape()[1:])
      x = linear_unsafe([input_t, attns], input_t.get_shape()[1], True)
      call_cell = lambda: unsafe_decorator(cell)(x, state)

      if sequence_length is not None:
        output, new_state = rnn._rnn_step(
          time=time,
          sequence_length=sequence_length,
          min_sequence_length=min_sequence_length,
          max_sequence_length=max_sequence_length,
          zero_output=zero_output,
          state=state,
          call_cell=call_cell,
          state_size=state_size,
          skip_conditionals=True)
      else:
        output, new_state = call_cell()

      state_ta_t = state_ta_t.write(time, new_state)
      attn_weights_ta_t.write(time, attn_weights)
      new_attns, new_attn_weights = attention_(new_state, prev_weights=attn_weights)

      if output_projection is not None:
        with tf.variable_scope('attention_output_projection'):
          output = linear_unsafe([output, new_attns], output_size, True)

      output_ta_t = output_ta_t.write(time, output)
      return time + 1, new_state, new_attns, new_attn_weights, output_ta_t, state_ta_t, attn_weights_ta_t

    _, _, _, _, output_final_ta, state_final_ta, attn_weights_final = tf.while_loop(
      cond=lambda time, *_: time < time_steps,
      body=_time_step,
      loop_vars=(time, state, attns, attention_weights, output_ta, state_ta, attn_weights_ta),
      parallel_iterations=decoder.parallel_iterations,
      swap_memory=decoder.swap_memory)

    outputs = output_final_ta.pack()
    decoder_states = state_final_ta.pack()
    attention_weights = tf.concat(0, [tf.expand_dims(attention_weights, 0),
                                      attn_weights_final.pack()])

    return outputs, decoder_states, attention_weights


def beam_search_decoder(decoder_input, initial_state, attention_states, encoders, decoder, initial_state_attention,
                        output_projection=None, dropout=None, **kwargs):
  # FIXME: only works with attention
  # FIXME: only works with initial_state_attention set to True
  # FIXME: doesn't work with convolutional attention
  if decoder.get('embedding') is not None:
    embedding_initializer = decoder.embedding
    embedding_shape = None
  else:
    embedding_initializer = None
    embedding_shape = [decoder.vocab_size, decoder.embedding_size]

  with tf.device('/cpu:0'):
    embedding = get_variable_unsafe('embedding_{}'.format(decoder.name),
                                    shape=embedding_shape,
                                    initializer=embedding_initializer)
  if decoder.use_lstm:
    cell = rnn_cell.BasicLSTMCell(decoder.cell_size)
  else:
    cell = rnn_cell.GRUCell(decoder.cell_size)

  if dropout is not None:
    cell = rnn_cell.DropoutWrapper(cell, input_keep_prob=dropout)

  if decoder.layers > 1:
    cell = rnn_cell.MultiRNNCell([cell] * decoder.layers)

  if output_projection is None:
    cell = rnn_cell.OutputProjectionWrapper(cell, decoder.vocab_size)
    output_size = decoder.vocab_size
  else:
    output_size = cell.output_size
    proj_weights = tf.convert_to_tensor(output_projection[0], dtype=tf.float32)
    proj_weights.get_shape().assert_is_compatible_with([cell.output_size, decoder.vocab_size])
    proj_biases = tf.convert_to_tensor(output_projection[1], dtype=tf.float32)
    proj_biases.get_shape().assert_is_compatible_with([decoder.vocab_size])

  with tf.variable_scope('decoder_{}'.format(decoder.name)):
    decoder_input = tf.nn.embedding_lookup(embedding, decoder_input)

    attn_lengths = [tf.shape(states)[1] for states in attention_states]
    attn_size = sum(states.get_shape()[2].value for states in attention_states)
    hidden_states = [tf.expand_dims(states, 2) for states in attention_states]
    attention_ = functools.partial(multi_attention, hidden_states=hidden_states, encoders=encoders)

    state = initial_state
    if state.get_shape()[1] != cell.state_size:
      state = linear_unsafe(initial_state, cell.state_size, False, scope='initial_state_projection')

    batch_size = tf.shape(decoder_input)[0]
    attn_weights = [tf.zeros(tf.pack([batch_size, length])) for length in attn_lengths]

    if initial_state_attention:
      attns, attn_weights = attention_(state, prev_weights=attn_weights)
    else:
      attns = tf.zeros(tf.pack([batch_size, attn_size]), dtype=tf.float32)

    input_size = decoder_input.get_shape()[1]
    x = linear_unsafe([decoder_input, attns], input_size, True)
    cell_output, new_state = unsafe_decorator(cell)(x, state)
    new_attns, new_attn_weights = attention_(new_state, prev_weights=attn_weights)
    if output_projection is None:
      output = cell_output
    else:
      with tf.variable_scope('attention_output_projection'):
        output = linear_unsafe([cell_output, new_attns], output_size, True)

    beam_tensors = namedtuple('beam_tensors', 'state new_state attn_weights new_attn_weights attns new_attns')
    return output, beam_tensors(state, new_state, attn_weights, new_attn_weights, attns, new_attns)


def sequence_loss(logits, targets, weights,
                  average_across_timesteps=True, average_across_batch=True,
                  softmax_loss_function=None, name=None):
  with tf.op_scope([logits, targets, weights], name, "sequence_loss"):
    time_steps = tf.shape(targets)[0]
    batch_size = tf.shape(targets)[1]

    logits_ = tf.reshape(logits, tf.pack([time_steps * batch_size, logits.get_shape()[2].value]))
    targets_ = tf.reshape(targets, tf.pack([time_steps * batch_size]))

    if softmax_loss_function is None:
      crossent = tf.nn.sparse_softmax_cross_entropy_with_logits(logits_, targets_)
    else:
      crossent = softmax_loss_function(logits_, targets_)

    crossent = tf.reshape(crossent, tf.pack([time_steps, batch_size]))
    log_perp = tf.reduce_sum(crossent * weights, 0)

    if average_across_timesteps:
      total_size = tf.reduce_sum(weights, 0)
      total_size += 1e-12  # just to avoid division by 0 for all-0 weights
      log_perp /= total_size

    cost = tf.reduce_sum(log_perp)

    if average_across_batch:
      batch_size = tf.shape(targets)[1]
      return cost / tf.cast(batch_size, tf.float32)
    else:
      return cost
