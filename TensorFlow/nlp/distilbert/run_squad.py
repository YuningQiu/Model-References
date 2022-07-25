# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

###############################################################################
# Copyright (C) 2021-2022 Habana Labs, Ltd. an Intel Company
#
# Changes:
# - Added stdout/stderr flush in main
# - Added default values for environment variables:
#   - HABANA_INITIAL_WORKSPACE_SIZE_MB
#   - TF_DISABLE_MKL
#   - TF_BF16_CONVERSION
#   - TF_PRELIMINARY_CLUSTER_SIZE
#   - TF_DISABLE_SCOPED_ALLOCATOR
# - Added command-line flags:
#   - num_train_steps
#   - deterministic_run
#   - deterministic_seed
# - Wrapped horovod import with a try-catch block so that the useris not required
#   to install this library when the model is being run on a single card
###############################################################################

"""Run BERT on SQuAD 1.1 and SQuAD 2.0."""

from __future__ import division
from __future__ import print_function
from __future__ import absolute_import

import os
import sys
import six
import math
import json
import socket
import random
import collections

curr_path = os.path.abspath(os.path.dirname(__file__))
sys.path.append(os.path.join(curr_path, '..'))

import numpy as np
import tensorflow as tf

from habana_frameworks.tensorflow import load_habana_module
from TensorFlow.common.debug import dump_callback
from TensorFlow.common.tb_utils import write_hparams_v1, TBSummary
import modeling
import optimization
import utils.habana_hooks as habana_hooks
import data_preprocessing.tokenization as tokenization

try:
    import horovod.tensorflow as hvd
except ImportError:
    hvd = None

def horovod_enabled():
  return hvd is not None and hvd.is_initialized()

flags = tf.compat.v1.flags

FLAGS = flags.FLAGS


def init_squad_flags():
  # Required parameters
  flags.DEFINE_string("distilbert_config_file", None,
      "The config json file corresponding to the pre-trained DistilBERT model. "
      "This specifies the model architecture.")

  flags.DEFINE_string("vocab_file", None,
      "The vocabulary file that the DistilBERT model was trained on. "
      "Reuse the vocabulary file of BERT base model.")

  flags.DEFINE_string("output_dir", None,
      "The output directory where the model checkpoints will be written.")

  # Other parameters
  flags.DEFINE_string("train_file", None,
      "SQuAD json for training. E.g., train-v1.1.json")

  flags.DEFINE_string("predict_file", None,
      "SQuAD json for predictions. E.g., dev-v1.1.json or test-v1.1.json")

  flags.DEFINE_string("init_checkpoint", None,
      "Initial checkpoint (usually from a pre-trained DistilBERT model).")

  flags.DEFINE_bool("do_lower_case", True,
      "Whether to lower case the input text. Should be True for uncased "
      "models and False for cased models.")

  flags.DEFINE_integer("max_seq_length", 384,
      "The maximum total input sequence length after WordPiece tokenization. "
      "Sequences longer than this will be truncated, and sequences shorter "
      "than this will be padded.")

  flags.DEFINE_integer("doc_stride", 128,
      "When splitting up a long document into chunks, how much stride to "
      "take between chunks.")

  flags.DEFINE_integer("max_query_length", 64,
      "The maximum number of tokens for the question. Questions longer than "
      "this will be truncated to this length.")

  flags.DEFINE_bool("do_train", False, "Whether to run training.")

  flags.DEFINE_bool("do_predict", False, "Whether to run eval on the dev set.")

  flags.DEFINE_bool("do_eval", False, "Whether to run eval on the dev set.")

  flags.DEFINE_integer("train_batch_size", 32, "Total batch size for training.")

  flags.DEFINE_integer("predict_batch_size", 8, "Total batch size for predictions.")

  flags.DEFINE_float("learning_rate", 5e-5, "The initial learning rate for Adam.")

  flags.DEFINE_float("num_train_epochs", 2.0, "Total number of training epochs to perform.")

  flags.DEFINE_float("num_train_steps", None,
      "Total number of training steps to perform.")

  flags.DEFINE_float("warmup_proportion", 0.1,
      "Proportion of training to perform linear learning rate warmup for. "
      "E.g., 0.1 = 10% of training.")

  flags.DEFINE_integer("save_summary_steps", 1,
      "How often to save the summary data.")

  flags.DEFINE_integer("iterations_per_loop", 1000,
      "How many steps to make in each estimator call.")

  flags.DEFINE_integer("n_best_size", 20,
      "The total number of n-best predictions to generate in the "
      "nbest_predictions.json output file.")

  flags.DEFINE_integer("max_answer_length", 30,
      "The maximum length of an answer that can be generated. This is needed "
      "because the start and end predictions are not conditioned on one another.")

  flags.DEFINE_bool("use_tpu", False, "Whether to use TPU or GPU/CPU.")

  flags.DEFINE_string("tpu_name", None,
      "The Cloud TPU to use for training. This should be either the name "
      "used when creating the Cloud TPU, or a grpc://ip.address.of.tpu:8470 "
      "url.")

  flags.DEFINE_string("tpu_zone", None,
      "[Optional] GCE zone where the Cloud TPU is located in. If not "
      "specified, we will attempt to automatically detect the GCE project from "
      "metadata.")

  flags.DEFINE_string("gcp_project", None,
      "[Optional] Project name for the Cloud TPU-enabled project. If not "
      "specified, we will attempt to automatically detect the GCE project from "
      "metadata.")

  flags.DEFINE_string("master", None, "[Optional] TensorFlow master URL.")

  flags.DEFINE_integer("num_tpu_cores", 8,
      "Only used if `use_tpu` is True. Total number of TPU cores to use.")

  flags.DEFINE_bool("verbose_logging", False,
      "If true, all of the warnings related to data processing will be printed. "
      "A number of warnings are expected for a normal SQuAD evaluation.")

  flags.DEFINE_bool("version_2_with_negative", False,
      "If true, the SQuAD examples contain some that do not have an answer.")

  flags.DEFINE_float("null_score_diff_threshold", 0.0,
      "If null_score - best_non_null is greater than the threshold predict null.")

  flags.DEFINE_bool("cpu_only", False, "Whether to run on CPU.")

  flags.DEFINE_bool("deterministic_run", False,
      "If set run will be deterministic (set random seed, read dataset in single thread, disable dropout)")

  flags.DEFINE_integer("deterministic_seed", None,
      "Seed vaule to be used in deterministic mode for all pseudorandom sequences")

  flags.DEFINE_bool("use_horovod", False, "Run training using horovod.")

  flags.DEFINE_bool('horovod_hierarchical_allreduce', False,
      "Enables hierarchical allreduce in Horovod. "
      "The environment variable `HOROVOD_HIERARCHICAL_ALLREDUCE` will be set to `1`.")

  flags.DEFINE_string("bf16_config_path", None, "Defines config for tensor converson to bf16 data type")

  flags.DEFINE_bool('enable_scoped_allocator', False, "Enable scoped allocator optimization")

def set_random_seed(seed):
  tf.compat.v1.set_random_seed(seed)
  tf.random.set_seed(seed)
  random.seed(seed)
  np.random.seed(seed)

class SquadExample(object):
  """A single training/test example for simple sequence classification.

     For examples without an answer, the start and end position are -1.
  """

  def __init__(self,
               qas_id,
               question_text,
               doc_tokens,
               orig_answer_text=None,
               start_position=None,
               end_position=None,
               is_impossible=False):
    self.qas_id = qas_id
    self.question_text = question_text
    self.doc_tokens = doc_tokens
    self.orig_answer_text = orig_answer_text
    self.start_position = start_position
    self.end_position = end_position
    self.is_impossible = is_impossible

  def __str__(self):
    return self.__repr__()

  def __repr__(self):
    s = ""
    s += "qas_id: %s" % (tokenization.printable_text(self.qas_id))
    s += ", question_text: %s" % (
        tokenization.printable_text(self.question_text))
    s += ", doc_tokens: [%s]" % (" ".join(self.doc_tokens))
    if self.start_position:
      s += ", start_position: %d" % (self.start_position)
    if self.start_position:
      s += ", end_position: %d" % (self.end_position)
    if self.start_position:
      s += ", is_impossible: %r" % (self.is_impossible)
    return s


class InputFeatures(object):
  """A single set of features of data."""

  def __init__(self,
               unique_id,
               example_index,
               doc_span_index,
               tokens,
               token_to_orig_map,
               token_is_max_context,
               input_ids,
               input_mask,
               segment_ids,
               start_position=None,
               end_position=None,
               is_impossible=None):
    self.unique_id = unique_id
    self.example_index = example_index
    self.doc_span_index = doc_span_index
    self.tokens = tokens
    self.token_to_orig_map = token_to_orig_map
    self.token_is_max_context = token_is_max_context
    self.input_ids = input_ids
    self.input_mask = input_mask
    self.segment_ids = segment_ids
    self.start_position = start_position
    self.end_position = end_position
    self.is_impossible = is_impossible


def read_squad_examples(input_file, is_training, version_2_with_negative):
  """Read a SQuAD json file into a list of SquadExample."""
  with tf.io.gfile.GFile(input_file, "r") as reader:
    input_data = json.load(reader)["data"]

  def is_whitespace(c):
    if c == " " or c == "\t" or c == "\r" or c == "\n" or ord(c) == 0x202F:
      return True
    return False

  examples = []
  for entry in input_data:
    for paragraph in entry["paragraphs"]:
      paragraph_text = paragraph["context"]
      doc_tokens = []
      char_to_word_offset = []
      prev_is_whitespace = True
      for c in paragraph_text:
        if is_whitespace(c):
          prev_is_whitespace = True
        else:
          if prev_is_whitespace:
            doc_tokens.append(c)
          else:
            doc_tokens[-1] += c
          prev_is_whitespace = False
        char_to_word_offset.append(len(doc_tokens) - 1)

      for qa in paragraph["qas"]:
        qas_id = qa["id"]
        question_text = qa["question"]
        start_position = None
        end_position = None
        orig_answer_text = None
        is_impossible = False
        if is_training:

          if version_2_with_negative:
            is_impossible = qa["is_impossible"]
          if (len(qa["answers"]) != 1) and (not is_impossible):
            raise ValueError(
                "For training, each question should have exactly 1 answer.")
          if not is_impossible:
            answer = qa["answers"][0]
            orig_answer_text = answer["text"]
            answer_offset = answer["answer_start"]
            answer_length = len(orig_answer_text)
            start_position = char_to_word_offset[answer_offset]
            end_position = char_to_word_offset[answer_offset + answer_length -
                                               1]
            # Only add answers where the text can be exactly recovered from the
            # document. If this CAN'T happen it's likely due to weird Unicode
            # stuff so we will just skip the example.
            #
            # Note that this means for training mode, every example is NOT
            # guaranteed to be preserved.
            actual_text = " ".join(
                doc_tokens[start_position:(end_position + 1)])
            cleaned_answer_text = " ".join(
                tokenization.whitespace_tokenize(orig_answer_text))
            if actual_text.find(cleaned_answer_text) == -1:
              tf.compat.v1.logging.warning("Could not find answer: '%s' vs. '%s'",
                                           actual_text, cleaned_answer_text)
              continue
          else:
            start_position = -1
            end_position = -1
            orig_answer_text = ""

        example = SquadExample(
            qas_id=qas_id,
            question_text=question_text,
            doc_tokens=doc_tokens,
            orig_answer_text=orig_answer_text,
            start_position=start_position,
            end_position=end_position,
            is_impossible=is_impossible)
        examples.append(example)

  return examples


def convert_examples_to_features(examples, tokenizer, max_seq_length,
                                 doc_stride, max_query_length, is_training,
                                 output_fn):
  """Loads a data file into a list of `InputBatch`s."""

  unique_id = 1000000000

  for (example_index, example) in enumerate(examples):
    query_tokens = tokenizer.tokenize(example.question_text)

    if len(query_tokens) > max_query_length:
      query_tokens = query_tokens[0:max_query_length]

    tok_to_orig_index = []
    orig_to_tok_index = []
    all_doc_tokens = []
    for (i, token) in enumerate(example.doc_tokens):
      orig_to_tok_index.append(len(all_doc_tokens))
      sub_tokens = tokenizer.tokenize(token)
      for sub_token in sub_tokens:
        tok_to_orig_index.append(i)
        all_doc_tokens.append(sub_token)

    tok_start_position = None
    tok_end_position = None
    if is_training and example.is_impossible:
      tok_start_position = -1
      tok_end_position = -1
    if is_training and not example.is_impossible:
      tok_start_position = orig_to_tok_index[example.start_position]
      if example.end_position < len(example.doc_tokens) - 1:
        tok_end_position = orig_to_tok_index[example.end_position + 1] - 1
      else:
        tok_end_position = len(all_doc_tokens) - 1
      (tok_start_position, tok_end_position) = _improve_answer_span(
          all_doc_tokens, tok_start_position, tok_end_position, tokenizer,
          example.orig_answer_text)

    # The -3 accounts for [CLS], [SEP] and [SEP]
    max_tokens_for_doc = max_seq_length - len(query_tokens) - 3

    # We can have documents that are longer than the maximum sequence length.
    # To deal with this we do a sliding window approach, where we take chunks
    # of the up to our max length with a stride of `doc_stride`.
    _DocSpan = collections.namedtuple(  # pylint: disable=invalid-name
        "DocSpan", ["start", "length"])
    doc_spans = []
    start_offset = 0
    while start_offset < len(all_doc_tokens):
      length = len(all_doc_tokens) - start_offset
      if length > max_tokens_for_doc:
        length = max_tokens_for_doc
      doc_spans.append(_DocSpan(start=start_offset, length=length))
      if start_offset + length == len(all_doc_tokens):
        break
      start_offset += min(length, doc_stride)

    for (doc_span_index, doc_span) in enumerate(doc_spans):
      tokens = []
      token_to_orig_map = {}
      token_is_max_context = {}
      segment_ids = []
      tokens.append("[CLS]")
      segment_ids.append(0)
      for token in query_tokens:
        tokens.append(token)
        segment_ids.append(0)
      tokens.append("[SEP]")
      segment_ids.append(0)

      for i in range(doc_span.length):
        split_token_index = doc_span.start + i
        token_to_orig_map[len(tokens)] = tok_to_orig_index[split_token_index]

        is_max_context = _check_is_max_context(doc_spans, doc_span_index,
                                               split_token_index)
        token_is_max_context[len(tokens)] = is_max_context
        tokens.append(all_doc_tokens[split_token_index])
        segment_ids.append(1)
      tokens.append("[SEP]")
      segment_ids.append(1)

      input_ids = tokenizer.convert_tokens_to_ids(tokens)

      # The mask has 1 for real tokens and 0 for padding tokens. Only real
      # tokens are attended to.
      input_mask = [1] * len(input_ids)

      # Zero-pad up to the sequence length.
      while len(input_ids) < max_seq_length:
        input_ids.append(0)
        input_mask.append(0)
        segment_ids.append(0)

      assert len(input_ids) == max_seq_length
      assert len(input_mask) == max_seq_length
      assert len(segment_ids) == max_seq_length

      start_position = None
      end_position = None
      if is_training and not example.is_impossible:
        # For training, if our document chunk does not contain an annotation
        # we throw it out, since there is nothing to predict.
        doc_start = doc_span.start
        doc_end = doc_span.start + doc_span.length - 1
        out_of_span = False
        if not (tok_start_position >= doc_start and
                tok_end_position <= doc_end):
          out_of_span = True
        if out_of_span:
          start_position = 0
          end_position = 0
        else:
          doc_offset = len(query_tokens) + 2
          start_position = tok_start_position - doc_start + doc_offset
          end_position = tok_end_position - doc_start + doc_offset

      if is_training and example.is_impossible:
        start_position = 0
        end_position = 0

      if example_index < 20:
        tf.compat.v1.logging.info("*** Example ***")
        tf.compat.v1.logging.info("unique_id: %s" % (unique_id))
        tf.compat.v1.logging.info("example_index: %s" % (example_index))
        tf.compat.v1.logging.info("doc_span_index: %s" % (doc_span_index))
        tf.compat.v1.logging.info("tokens: %s" % " ".join(
            [tokenization.printable_text(x) for x in tokens]))
        tf.compat.v1.logging.info("token_to_orig_map: %s" % " ".join(
            ["%d:%d" % (x, y) for (x, y) in six.iteritems(token_to_orig_map)]))
        tf.compat.v1.logging.info("token_is_max_context: %s" % " ".join([
            "%d:%s" % (x, y) for (x, y) in six.iteritems(token_is_max_context)
        ]))
        tf.compat.v1.logging.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
        tf.compat.v1.logging.info(
            "input_mask: %s" % " ".join([str(x) for x in input_mask]))
        tf.compat.v1.logging.info(
            "segment_ids: %s" % " ".join([str(x) for x in segment_ids]))
        if is_training and example.is_impossible:
          tf.compat.v1.logging.info("impossible example")
        if is_training and not example.is_impossible:
          answer_text = " ".join(tokens[start_position:(end_position + 1)])
          tf.compat.v1.logging.info("start_position: %d" % (start_position))
          tf.compat.v1.logging.info("end_position: %d" % (end_position))
          tf.compat.v1.logging.info(
              "answer: %s" % (tokenization.printable_text(answer_text)))

      feature = InputFeatures(
          unique_id=unique_id,
          example_index=example_index,
          doc_span_index=doc_span_index,
          tokens=tokens,
          token_to_orig_map=token_to_orig_map,
          token_is_max_context=token_is_max_context,
          input_ids=input_ids,
          input_mask=input_mask,
          segment_ids=segment_ids,
          start_position=start_position,
          end_position=end_position,
          is_impossible=example.is_impossible)

      # Run callback
      output_fn(feature)

      unique_id += 1


def _improve_answer_span(doc_tokens, input_start, input_end, tokenizer,
                         orig_answer_text):
  """Returns tokenized answer spans that better match the annotated answer."""

  # The SQuAD annotations are character based. We first project them to
  # whitespace-tokenized words. But then after WordPiece tokenization, we can
  # often find a "better match". For example:
  #
  #   Question: What year was John Smith born?
  #   Context: The leader was John Smith (1895-1943).
  #   Answer: 1895
  #
  # The original whitespace-tokenized answer will be "(1895-1943).". However
  # after tokenization, our tokens will be "( 1895 - 1943 ) .". So we can match
  # the exact answer, 1895.
  #
  # However, this is not always possible. Consider the following:
  #
  #   Question: What country is the top exporter of electornics?
  #   Context: The Japanese electronics industry is the lagest in the world.
  #   Answer: Japan
  #
  # In this case, the annotator chose "Japan" as a character sub-span of
  # the word "Japanese". Since our WordPiece tokenizer does not split
  # "Japanese", we just use "Japanese" as the annotation. This is fairly rare
  # in SQuAD, but does happen.
  tok_answer_text = " ".join(tokenizer.tokenize(orig_answer_text))

  for new_start in range(input_start, input_end + 1):
    for new_end in range(input_end, new_start - 1, -1):
      text_span = " ".join(doc_tokens[new_start:(new_end + 1)])
      if text_span == tok_answer_text:
        return (new_start, new_end)

  return (input_start, input_end)


def _check_is_max_context(doc_spans, cur_span_index, position):
  """Check if this is the 'max context' doc span for the token."""

  # Because of the sliding window approach taken to scoring documents, a single
  # token can appear in multiple documents. E.g.
  #  Doc: the man went to the store and bought a gallon of milk
  #  Span A: the man went to the
  #  Span B: to the store and bought
  #  Span C: and bought a gallon of
  #  ...
  #
  # Now the word 'bought' will have two scores from spans B and C. We only
  # want to consider the score with "maximum context", which we define as
  # the *minimum* of its left and right context (the *sum* of left and
  # right context will always be the same, of course).
  #
  # In the example the maximum context for 'bought' would be span C since
  # it has 1 left context and 3 right context, while span B has 4 left context
  # and 0 right context.
  best_score = None
  best_span_index = None
  for (span_index, doc_span) in enumerate(doc_spans):
    end = doc_span.start + doc_span.length - 1
    if position < doc_span.start:
      continue
    if position > end:
      continue
    num_left_context = position - doc_span.start
    num_right_context = end - position
    score = min(num_left_context, num_right_context) + 0.01 * doc_span.length
    if best_score is None or score > best_score:
      best_score = score
      best_span_index = span_index

  return cur_span_index == best_span_index


def create_model(bert_config, is_training, input_ids, input_mask, segment_ids,
                 use_one_hot_embeddings):
  """Creates a classification model."""
  model = modeling.BertModel(
      config=bert_config,
      is_training=is_training,
      input_ids=input_ids,
      input_mask=input_mask,
      token_type_ids=segment_ids,
      use_one_hot_embeddings=use_one_hot_embeddings)

  final_hidden = model.get_sequence_output()

  final_hidden_shape = modeling.get_shape_list(final_hidden, expected_rank=3)
  batch_size = final_hidden_shape[0]
  seq_length = final_hidden_shape[1]
  hidden_size = final_hidden_shape[2]

  output_weights = tf.compat.v1.get_variable(
      "cls/squad/output_weights", [2, hidden_size],
      initializer=tf.compat.v1.truncated_normal_initializer(stddev=0.02))

  output_bias = tf.compat.v1.get_variable(
      "cls/squad/output_bias", [2], initializer=tf.compat.v1.zeros_initializer())

  final_hidden_matrix = tf.reshape(final_hidden,
                                   [batch_size * seq_length, hidden_size])
  logits = tf.matmul(final_hidden_matrix, output_weights, transpose_b=True)
  logits = tf.nn.bias_add(logits, output_bias)

  logits = tf.reshape(logits, [batch_size, seq_length, 2])
  logits = tf.transpose(a=logits, perm=[2, 0, 1])

  unstacked_logits = tf.unstack(logits, axis=0)

  (start_logits, end_logits) = (unstacked_logits[0], unstacked_logits[1])

  return (start_logits, end_logits)


def model_fn_builder(bert_config, init_checkpoint, learning_rate,
                     num_train_steps, num_warmup_steps, use_tpu,
                     use_one_hot_embeddings):
  """Returns `model_fn` closure for TPUEstimator."""

  def model_fn(features, labels, mode, params):  # pylint: disable=unused-argument
    """The `model_fn` for TPUEstimator."""

    tf.compat.v1.logging.info("*** Features ***")
    for name in sorted(features.keys()):
      tf.compat.v1.logging.info("  name = %s, shape = %s" % (name, features[name].shape))

    unique_ids = features["unique_ids"]
    input_ids = features["input_ids"]
    input_mask = features["input_mask"]
    segment_ids = features["segment_ids"]

    is_training = (mode == tf.estimator.ModeKeys.TRAIN)

    (start_logits, end_logits) = create_model(
        bert_config=bert_config,
        is_training=is_training,
        input_ids=input_ids,
        input_mask=input_mask,
        segment_ids=segment_ids,
        use_one_hot_embeddings=use_one_hot_embeddings)

    tvars = tf.compat.v1.trainable_variables()

    initialized_variable_names = {}
    scaffold_fn = None
    if init_checkpoint:
      (assignment_map, initialized_variable_names
       ) = modeling.get_assignment_map_from_checkpoint(tvars, init_checkpoint)
      if use_tpu:

        def tpu_scaffold():
          tf.compat.v1.train.init_from_checkpoint(init_checkpoint, assignment_map)
          return tf.compat.v1.train.Scaffold()

        scaffold_fn = tpu_scaffold
      else:
        tf.compat.v1.train.init_from_checkpoint(init_checkpoint, assignment_map)

    tf.compat.v1.logging.info("**** Trainable Variables ****")
    for var in tvars:
      init_string = ""
      if var.name in initialized_variable_names:
        init_string = ", *INIT_FROM_CKPT*"
      tf.compat.v1.logging.info("  name = %s, shape = %s%s", var.name, var.shape,
                                init_string)

    output_spec = None
    if mode == tf.estimator.ModeKeys.TRAIN:
      seq_length = modeling.get_shape_list(input_ids)[1]

      def compute_loss(logits, positions):
        one_hot_positions = tf.one_hot(
            positions, depth=seq_length, dtype=tf.float32)
        log_probs = tf.nn.log_softmax(logits, axis=-1)
        loss = -tf.reduce_mean(
            input_tensor=tf.reduce_sum(input_tensor=one_hot_positions * log_probs, axis=-1))
        return loss

      start_positions = features["start_positions"]
      end_positions = features["end_positions"]

      start_loss = compute_loss(start_logits, start_positions)
      end_loss = compute_loss(end_logits, end_positions)

      total_loss = (start_loss + end_loss) / 2.0
      total_loss = tf.compat.v1.Print(total_loss, [total_loss], message="LOSS: ")

      train_op = optimization.create_optimizer(
          total_loss, learning_rate, num_train_steps, num_warmup_steps, use_tpu)

      output_spec = tf.compat.v1.estimator.tpu.TPUEstimatorSpec(
          mode=mode,
          loss=total_loss,
          train_op=train_op,
          scaffold_fn=scaffold_fn)
    elif mode == tf.estimator.ModeKeys.PREDICT:
      predictions = {
          "unique_ids": unique_ids,
          "start_logits": start_logits,
          "end_logits": end_logits,
      }
      output_spec = tf.compat.v1.estimator.tpu.TPUEstimatorSpec(
          mode=mode, predictions=predictions, scaffold_fn=scaffold_fn)
    else:
      raise ValueError(
          "Only TRAIN and PREDICT modes are supported: %s" % (mode))

    return output_spec

  return model_fn


def input_fn_builder(input_file, seq_length, is_training, drop_remainder):
  """Creates an `input_fn` closure to be passed to TPUEstimator."""

  name_to_features = {
      "unique_ids": tf.io.FixedLenFeature([], tf.int64),
      "input_ids": tf.io.FixedLenFeature([seq_length], tf.int64),
      "input_mask": tf.io.FixedLenFeature([seq_length], tf.int64),
      "segment_ids": tf.io.FixedLenFeature([seq_length], tf.int64),
  }

  if is_training:
    name_to_features["start_positions"] = tf.io.FixedLenFeature([], tf.int64)
    name_to_features["end_positions"] = tf.io.FixedLenFeature([], tf.int64)

  def _decode_record(record, name_to_features):
    """Decodes a record to a TensorFlow example."""
    example = tf.io.parse_single_example(serialized=record, features=name_to_features)

    # tf.Example only supports tf.int64, but the TPU only supports tf.int32.
    # So cast all int64 to int32.
    for name in list(example.keys()):
      t = example[name]
      if t.dtype == tf.int64:
        t = tf.cast(t, dtype=tf.int32)
      example[name] = t

    return example

  def input_fn(params):
    """The actual input function."""
    batch_size = params["batch_size"]

    # For training, we want a lot of parallel reading and shuffling.
    # For eval, we want no shuffling and parallel reading doesn't matter.
    d = tf.data.TFRecordDataset(input_file)

    if FLAGS.deterministic_run:
      d = d.repeat()
      d = d.shuffle(buffer_size=100, seed=FLAGS.deterministic_seed)
      d = d.apply(
          tf.data.experimental.map_and_batch(
              lambda record: _decode_record(record, name_to_features),
              batch_size=batch_size,
              num_parallel_calls=1,
              drop_remainder=drop_remainder))

    else:
      if is_training:
        if horovod_enabled():
          d = d.shard(hvd.local_size(), hvd.local_rank())
        d = d.repeat()
        d = d.shuffle(buffer_size=100, seed=FLAGS.deterministic_seed)

      d = d.apply(
          tf.data.experimental.map_and_batch(
              lambda record: _decode_record(record, name_to_features),
              batch_size=batch_size,
              drop_remainder=drop_remainder))

    d = d.prefetch(tf.data.experimental.AUTOTUNE)
    return d

  return input_fn


RawResult = collections.namedtuple("RawResult",
                                   ["unique_id", "start_logits", "end_logits"])


def write_predictions(all_examples, all_features, all_results, n_best_size,
                      max_answer_length, do_lower_case, output_prediction_file,
                      output_nbest_file, output_null_log_odds_file, version_2_with_negative,
                      verbose_logging):
  """Write final predictions to the json file and log-odds of null if needed."""
  tf.compat.v1.logging.info("Writing predictions to: %s" % (output_prediction_file))
  tf.compat.v1.logging.info("Writing nbest to: %s" % (output_nbest_file))

  example_index_to_features = collections.defaultdict(list)
  for feature in all_features:
    example_index_to_features[feature.example_index].append(feature)

  unique_id_to_result = {}
  for result in all_results:
    unique_id_to_result[result.unique_id] = result

  _PrelimPrediction = collections.namedtuple(  # pylint: disable=invalid-name
      "PrelimPrediction",
      ["feature_index", "start_index", "end_index", "start_logit", "end_logit"])

  all_predictions = collections.OrderedDict()
  all_nbest_json = collections.OrderedDict()
  scores_diff_json = collections.OrderedDict()

  for (example_index, example) in enumerate(all_examples):
    features = example_index_to_features[example_index]

    prelim_predictions = []
    # keep track of the minimum score of null start+end of position 0
    score_null = 1000000  # large and positive
    min_null_feature_index = 0  # the paragraph slice with min mull score
    null_start_logit = 0  # the start logit at the slice with min null score
    null_end_logit = 0  # the end logit at the slice with min null score
    for (feature_index, feature) in enumerate(features):
      result = unique_id_to_result[feature.unique_id]
      start_indexes = _get_best_indexes(result.start_logits, n_best_size)
      end_indexes = _get_best_indexes(result.end_logits, n_best_size)
      # if we could have irrelevant answers, get the min score of irrelevant
      if version_2_with_negative:
        feature_null_score = result.start_logits[0] + result.end_logits[0]
        if feature_null_score < score_null:
          score_null = feature_null_score
          min_null_feature_index = feature_index
          null_start_logit = result.start_logits[0]
          null_end_logit = result.end_logits[0]
      for start_index in start_indexes:
        for end_index in end_indexes:
          # We could hypothetically create invalid predictions, e.g., predict
          # that the start of the span is in the question. We throw out all
          # invalid predictions.
          if start_index >= len(feature.tokens):
            continue
          if end_index >= len(feature.tokens):
            continue
          if start_index not in feature.token_to_orig_map:
            continue
          if end_index not in feature.token_to_orig_map:
            continue
          if not feature.token_is_max_context.get(start_index, False):
            continue
          if end_index < start_index:
            continue
          length = end_index - start_index + 1
          if length > max_answer_length:
            continue
          prelim_predictions.append(
              _PrelimPrediction(
                  feature_index=feature_index,
                  start_index=start_index,
                  end_index=end_index,
                  start_logit=result.start_logits[start_index],
                  end_logit=result.end_logits[end_index]))

    if version_2_with_negative:
      prelim_predictions.append(
          _PrelimPrediction(
              feature_index=min_null_feature_index,
              start_index=0,
              end_index=0,
              start_logit=null_start_logit,
              end_logit=null_end_logit))
    prelim_predictions = sorted(
        prelim_predictions,
        key=lambda x: (x.start_logit + x.end_logit),
        reverse=True)

    _NbestPrediction = collections.namedtuple(  # pylint: disable=invalid-name
        "NbestPrediction", ["text", "start_logit", "end_logit"])

    seen_predictions = {}
    nbest = []
    for pred in prelim_predictions:
      if len(nbest) >= n_best_size:
        break
      feature = features[pred.feature_index]
      if pred.start_index > 0:  # this is a non-null prediction
        tok_tokens = feature.tokens[pred.start_index:(pred.end_index + 1)]
        orig_doc_start = feature.token_to_orig_map[pred.start_index]
        orig_doc_end = feature.token_to_orig_map[pred.end_index]
        orig_tokens = example.doc_tokens[orig_doc_start:(orig_doc_end + 1)]
        tok_text = " ".join(tok_tokens)

        # De-tokenize WordPieces that have been split off.
        tok_text = tok_text.replace(" ##", "")
        tok_text = tok_text.replace("##", "")

        # Clean whitespace
        tok_text = tok_text.strip()
        tok_text = " ".join(tok_text.split())
        orig_text = " ".join(orig_tokens)

        final_text = get_final_text(tok_text, orig_text, do_lower_case, verbose_logging)
        if final_text in seen_predictions:
          continue

        seen_predictions[final_text] = True
      else:
        final_text = ""
        seen_predictions[final_text] = True

      nbest.append(
          _NbestPrediction(
              text=final_text,
              start_logit=pred.start_logit,
              end_logit=pred.end_logit))

    # if we didn't inlude the empty option in the n-best, inlcude it
    if version_2_with_negative:
      if "" not in seen_predictions:
        nbest.append(
            _NbestPrediction(
                text="", start_logit=null_start_logit,
                end_logit=null_end_logit))
    # In very rare edge cases we could have no valid predictions. So we
    # just create a nonce prediction in this case to avoid failure.
    if not nbest:
      nbest.append(
          _NbestPrediction(text="empty", start_logit=0.0, end_logit=0.0))

    assert len(nbest) >= 1

    total_scores = []
    best_non_null_entry = None
    for entry in nbest:
      total_scores.append(entry.start_logit + entry.end_logit)
      if not best_non_null_entry:
        if entry.text:
          best_non_null_entry = entry

    probs = _compute_softmax(total_scores)

    nbest_json = []
    for (i, entry) in enumerate(nbest):
      output = collections.OrderedDict()
      output["text"] = entry.text
      output["probability"] = probs[i]
      output["start_logit"] = entry.start_logit
      output["end_logit"] = entry.end_logit
      nbest_json.append(output)

    assert len(nbest_json) >= 1

    if not version_2_with_negative:
      all_predictions[example.qas_id] = nbest_json[0]["text"]
    else:
      # predict "" iff the null score - the score of best non-null > threshold
      score_diff = score_null - best_non_null_entry.start_logit - (
          best_non_null_entry.end_logit)
      scores_diff_json[example.qas_id] = score_diff
      if score_diff > FLAGS.null_score_diff_threshold:
        all_predictions[example.qas_id] = ""
      else:
        all_predictions[example.qas_id] = best_non_null_entry.text

    all_nbest_json[example.qas_id] = nbest_json

  with tf.io.gfile.GFile(output_prediction_file, "w") as writer:
    writer.write(json.dumps(all_predictions, indent=4) + "\n")

  with tf.io.gfile.GFile(output_nbest_file, "w") as writer:
    writer.write(json.dumps(all_nbest_json, indent=4) + "\n")

  if version_2_with_negative:
    with tf.io.gfile.GFile(output_null_log_odds_file, "w") as writer:
      writer.write(json.dumps(scores_diff_json, indent=4) + "\n")

  return [all_predictions, all_nbest_json, scores_diff_json]


def get_final_text(pred_text, orig_text, do_lower_case, verbose_logging):
  """Project the tokenized prediction back to the original text."""

  # When we created the data, we kept track of the alignment between original
  # (whitespace tokenized) tokens and our WordPiece tokenized tokens. So
  # now `orig_text` contains the span of our original text corresponding to the
  # span that we predicted.
  #
  # However, `orig_text` may contain extra characters that we don't want in
  # our prediction.
  #
  # For example, let's say:
  #   pred_text = steve smith
  #   orig_text = Steve Smith's
  #
  # We don't want to return `orig_text` because it contains the extra "'s".
  #
  # We don't want to return `pred_text` because it's already been normalized
  # (the SQuAD eval script also does punctuation stripping/lower casing but
  # our tokenizer does additional normalization like stripping accent
  # characters).
  #
  # What we really want to return is "Steve Smith".
  #
  # Therefore, we have to apply a semi-complicated alignment heruistic between
  # `pred_text` and `orig_text` to get a character-to-charcter alignment. This
  # can fail in certain cases in which case we just return `orig_text`.

  def _strip_spaces(text):
    ns_chars = []
    ns_to_s_map = collections.OrderedDict()
    for (i, c) in enumerate(text):
      if c == " ":
        continue
      ns_to_s_map[len(ns_chars)] = i
      ns_chars.append(c)
    ns_text = "".join(ns_chars)
    return (ns_text, ns_to_s_map)

  # We first tokenize `orig_text`, strip whitespace from the result
  # and `pred_text`, and check if they are the same length. If they are
  # NOT the same length, the heuristic has failed. If they are the same
  # length, we assume the characters are one-to-one aligned.
  tokenizer = tokenization.BasicTokenizer(do_lower_case=do_lower_case)

  tok_text = " ".join(tokenizer.tokenize(orig_text))

  start_position = tok_text.find(pred_text)
  if start_position == -1:
    if verbose_logging:
      tf.compat.v1.logging.info(
          "Unable to find text: '%s' in '%s'" % (pred_text, orig_text))
    return orig_text
  end_position = start_position + len(pred_text) - 1

  (orig_ns_text, orig_ns_to_s_map) = _strip_spaces(orig_text)
  (tok_ns_text, tok_ns_to_s_map) = _strip_spaces(tok_text)

  if len(orig_ns_text) != len(tok_ns_text):
    if verbose_logging:
      tf.compat.v1.logging.info("Length not equal after stripping spaces: '%s' vs '%s'",
                                orig_ns_text, tok_ns_text)
    return orig_text

  # We then project the characters in `pred_text` back to `orig_text` using
  # the character-to-character alignment.
  tok_s_to_ns_map = {}
  for (i, tok_index) in six.iteritems(tok_ns_to_s_map):
    tok_s_to_ns_map[tok_index] = i

  orig_start_position = None
  if start_position in tok_s_to_ns_map:
    ns_start_position = tok_s_to_ns_map[start_position]
    if ns_start_position in orig_ns_to_s_map:
      orig_start_position = orig_ns_to_s_map[ns_start_position]

  if orig_start_position is None:
    if verbose_logging:
      tf.compat.v1.logging.info("Couldn't map start position")
    return orig_text

  orig_end_position = None
  if end_position in tok_s_to_ns_map:
    ns_end_position = tok_s_to_ns_map[end_position]
    if ns_end_position in orig_ns_to_s_map:
      orig_end_position = orig_ns_to_s_map[ns_end_position]

  if orig_end_position is None:
    if verbose_logging:
      tf.compat.v1.logging.info("Couldn't map end position")
    return orig_text

  output_text = orig_text[orig_start_position:(orig_end_position + 1)]
  return output_text


def _get_best_indexes(logits, n_best_size):
  """Get the n-best logits from a list."""
  index_and_score = sorted(enumerate(logits), key=lambda x: x[1], reverse=True)

  best_indexes = []
  for i in range(len(index_and_score)):
    if i >= n_best_size:
      break
    best_indexes.append(index_and_score[i][0])
  return best_indexes


def _compute_softmax(scores):
  """Compute softmax probability over raw logits."""
  if not scores:
    return []

  max_score = None
  for score in scores:
    if max_score is None or score > max_score:
      max_score = score

  exp_scores = []
  total_sum = 0.0
  for score in scores:
    x = math.exp(score - max_score)
    exp_scores.append(x)
    total_sum += x

  probs = []
  for score in exp_scores:
    probs.append(score / total_sum)
  return probs


class FeatureWriter(object):
  """Writes InputFeature to TF example file."""

  def __init__(self, filename, is_training):
    self.filename = filename
    self.is_training = is_training
    self.num_features = 0
    self._writer = tf.io.TFRecordWriter(filename)

  def process_feature(self, feature):
    """Write a InputFeature to the TFRecordWriter as a tf.train.Example."""
    self.num_features += 1

    def create_int_feature(values):
      feature = tf.train.Feature(
          int64_list=tf.train.Int64List(value=list(values)))
      return feature

    features = collections.OrderedDict()
    features["unique_ids"] = create_int_feature([feature.unique_id])
    features["input_ids"] = create_int_feature(feature.input_ids)
    features["input_mask"] = create_int_feature(feature.input_mask)
    features["segment_ids"] = create_int_feature(feature.segment_ids)

    if self.is_training:
      features["start_positions"] = create_int_feature([feature.start_position])
      features["end_positions"] = create_int_feature([feature.end_position])
      impossible = 0
      if feature.is_impossible:
        impossible = 1
      features["is_impossible"] = create_int_feature([impossible])

    tf_example = tf.train.Example(features=tf.train.Features(feature=features))
    self._writer.write(tf_example.SerializeToString())

  def close(self):
    self._writer.close()


def validate_flags_or_throw(bert_config):
  """Validate the input FLAGS or throw an exception."""
  tokenization.validate_case_matches_checkpoint(FLAGS.do_lower_case,
                                                FLAGS.init_checkpoint)

  if not FLAGS.do_train and not FLAGS.do_predict:
    raise ValueError("At least one of `do_train` or `do_predict` must be True.")

  if FLAGS.do_train:
    if not FLAGS.train_file:
      raise ValueError(
          "If `do_train` is True, then `train_file` must be specified.")
  if FLAGS.do_predict:
    if not FLAGS.predict_file:
      raise ValueError(
          "If `do_predict` is True, then `predict_file` must be specified.")

  if FLAGS.max_seq_length > bert_config.max_position_embeddings:
    raise ValueError(
        "Cannot use sequence length %d because the BERT model "
        "was only trained up to sequence length %d" %
        (FLAGS.max_seq_length, bert_config.max_position_embeddings))

  if FLAGS.max_seq_length <= FLAGS.max_query_length + 3:
    raise ValueError(
        "The max_seq_length (%d) must be greater than max_query_length "
        "(%d) + 3" % (FLAGS.max_seq_length, FLAGS.max_query_length))


def main(_):

  if FLAGS.deterministic_seed:
    set_random_seed(FLAGS.deterministic_seed)

  tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.INFO)

  bert_config = modeling.BertConfig.from_json_file(FLAGS.distilbert_config_file)

  validate_flags_or_throw(bert_config)

  tf.io.gfile.makedirs(FLAGS.output_dir)

  tokenizer = tokenization.FullTokenizer(
      vocab_file=FLAGS.vocab_file, do_lower_case=FLAGS.do_lower_case)

  tpu_cluster_resolver = None
  if FLAGS.use_tpu and FLAGS.tpu_name:
    tpu_cluster_resolver = tf.distribute.cluster_resolver.TPUClusterResolver(
        FLAGS.tpu_name, zone=FLAGS.tpu_zone, project=FLAGS.gcp_project)

  model_dir = FLAGS.output_dir
  if horovod_enabled():
    model_dir = os.path.join(FLAGS.output_dir, "worker_" + str(hvd.rank()))

  is_per_host = tf.compat.v1.estimator.tpu.InputPipelineConfig.PER_HOST_V2

  # The Scoped Allocator Optimization is enabled by default unless disabled by a flag.
  if FLAGS.enable_scoped_allocator:
    from tensorflow.core.protobuf import rewriter_config_pb2  # pylint: disable=import-error

    session_config = tf.compat.v1.ConfigProto()
    session_config.graph_options.rewrite_options.scoped_allocator_optimization = rewriter_config_pb2.RewriterConfig.ON

    enable_op = session_config.graph_options.rewrite_options.scoped_allocator_opts.enable_op
    del enable_op[:]
    enable_op.append("HorovodAllreduce")
  else:
    session_config = None

  run_config = tf.compat.v1.estimator.tpu.RunConfig(
      cluster=tpu_cluster_resolver,
      master=FLAGS.master,
      model_dir=model_dir,
      keep_checkpoint_max=1,
      save_summary_steps=FLAGS.save_summary_steps,
      tpu_config=tf.compat.v1.estimator.tpu.TPUConfig(
          iterations_per_loop=FLAGS.iterations_per_loop,
          num_shards=FLAGS.num_tpu_cores,
          per_host_input_for_training=is_per_host),
      session_config=session_config)

  train_examples = None
  num_train_steps = None
  num_warmup_steps = None

  train_batch_size = FLAGS.train_batch_size
  if horovod_enabled():
    train_batch_size = train_batch_size * hvd.size()

  if FLAGS.do_train:
    train_examples = read_squad_examples(
        input_file=FLAGS.train_file, is_training=True, version_2_with_negative=FLAGS.version_2_with_negative)
    if FLAGS.num_train_steps is not None:
      num_train_steps = FLAGS.num_train_steps
    else:
      num_train_steps = int(
          len(train_examples) / train_batch_size * FLAGS.num_train_epochs)
    num_warmup_steps = int(num_train_steps * FLAGS.warmup_proportion)

    # Pre-shuffle the input to avoid having to make a very large shuffle
    # buffer in in the `input_fn`.
    rng = random.Random(12345)
    rng.shuffle(train_examples)

  start_index = 0
  end_index = len(train_examples)
  per_worker_filenames = [os.path.join(FLAGS.output_dir, "train.tf_record")]
  worker_id = 0

  if horovod_enabled():
    per_worker_filenames = [os.path.join(FLAGS.output_dir, "train.tf_record_{}".format(i))
                            for i in range(hvd.local_size())]
    num_examples_per_rank = len(train_examples) // hvd.size()
    remainder = len(train_examples) % hvd.size()
    worker_id = hvd.rank()
    if worker_id < remainder:
      start_index = worker_id * (num_examples_per_rank + 1)
      end_index = start_index + num_examples_per_rank + 1
    else:
      start_index = worker_id * num_examples_per_rank + remainder
      end_index = start_index + (num_examples_per_rank)

  learning_rate = FLAGS.learning_rate
  if horovod_enabled():
    learning_rate = learning_rate * hvd.size()

  model_fn = model_fn_builder(
      bert_config=bert_config,
      init_checkpoint=FLAGS.init_checkpoint,
      learning_rate=FLAGS.learning_rate,
      num_train_steps=num_train_steps,
      num_warmup_steps=num_warmup_steps,
      use_tpu=FLAGS.use_tpu,
      use_one_hot_embeddings=FLAGS.use_tpu)

  # If TPU is not available, this will fall back to normal Estimator on CPU
  # or GPU.
  estimator = tf.compat.v1.estimator.tpu.TPUEstimator(
      use_tpu=FLAGS.use_tpu,
      model_fn=model_fn,
      config=run_config,
      train_batch_size=FLAGS.train_batch_size,
      predict_batch_size=FLAGS.predict_batch_size)

  write_hparams_v1(FLAGS.output_dir, {
    'batch_size': FLAGS.train_batch_size,
    **{x: getattr(FLAGS, x) for x in FLAGS}
  })

  if FLAGS.do_train:
    # We write to a temporary file to avoid storing very large constant tensors
    # in memory.

    train_writer = FeatureWriter(
        filename=per_worker_filenames[hvd.local_rank() if horovod_enabled() else worker_id],
        is_training=True)
    convert_examples_to_features(
        examples=train_examples[start_index:end_index],
        tokenizer=tokenizer,
        max_seq_length=FLAGS.max_seq_length,
        doc_stride=FLAGS.doc_stride,
        max_query_length=FLAGS.max_query_length,
        is_training=True,
        output_fn=train_writer.process_feature)
    train_writer.close()
    num_features = train_writer.num_features

    tf.compat.v1.logging.info("***** Running training *****")
    tf.compat.v1.logging.info("  Num orig examples = %d", len(train_examples))
    tf.compat.v1.logging.info("  Num split examples = %d", num_features)
    tf.compat.v1.logging.info("  Per-worker batch size = %d", FLAGS.train_batch_size)
    tf.compat.v1.logging.info("  Total batch size = %d", train_batch_size)
    tf.compat.v1.logging.info("  Num steps = %d", num_train_steps)
    del train_examples

    train_input_fn = input_fn_builder(
        input_file=per_worker_filenames,
        seq_length=FLAGS.max_seq_length,
        is_training=True,
        drop_remainder=True)

    train_hooks = [habana_hooks.PerfLoggingHook(batch_size=train_batch_size, mode="train")]
    if horovod_enabled():
      train_hooks.append(hvd.BroadcastGlobalVariablesHook(0))

    if "range" == os.environ.get("HABANA_SYNAPSE_LOGGER", "False").lower():
      from habana_frameworks.tensorflow.synapse_logger_helpers import SynapseLoggerHook
      begin = 670
      end = begin + 10
      print("Begin: {}".format(begin))
      print("End: {}".format(end))
      train_hooks.append(SynapseLoggerHook(list(range(begin, end)), False))

    with dump_callback():
      estimator.train(input_fn=train_input_fn, max_steps=num_train_steps, hooks=train_hooks)

  if FLAGS.do_predict:
    eval_examples = read_squad_examples(
        input_file=FLAGS.predict_file, is_training=False, version_2_with_negative=FLAGS.version_2_with_negative)

    eval_writer = FeatureWriter(
        filename=os.path.join(model_dir, "eval.tf_record"),
        is_training=False)
    eval_features = []

    def append_feature(feature):
      eval_features.append(feature)
      eval_writer.process_feature(feature)

    convert_examples_to_features(
        examples=eval_examples,
        tokenizer=tokenizer,
        max_seq_length=FLAGS.max_seq_length,
        doc_stride=FLAGS.doc_stride,
        max_query_length=FLAGS.max_query_length,
        is_training=False,
        output_fn=append_feature)
    eval_writer.close()

    tf.compat.v1.logging.info("***** Running predictions *****")
    tf.compat.v1.logging.info("  Num orig examples = %d", len(eval_examples))
    tf.compat.v1.logging.info("  Num split examples = %d", len(eval_features))
    tf.compat.v1.logging.info("  Batch size = %d", FLAGS.predict_batch_size)

    all_results = []

    predict_input_fn = input_fn_builder(
        input_file=eval_writer.filename,
        seq_length=FLAGS.max_seq_length,
        is_training=False,
        drop_remainder=False)

    eval_hooks = [habana_hooks.PerfLoggingHook(batch_size=FLAGS.predict_batch_size, mode="eval")]

    # If running eval on the TPU, you will need to specify the number of
    # steps.
    all_results = []
    for result in estimator.predict(
            predict_input_fn, yield_single_examples=True, hooks=eval_hooks):
      if len(all_results) % 1000 == 0:
        tf.compat.v1.logging.info("Processing example: %d" % (len(all_results)))
      unique_id = int(result["unique_ids"])
      start_logits = [float(x) for x in result["start_logits"].flat]
      end_logits = [float(x) for x in result["end_logits"].flat]
      all_results.append(
          RawResult(
              unique_id=unique_id,
              start_logits=start_logits,
              end_logits=end_logits))

    output_prediction_file = os.path.join(model_dir, "predictions.json")
    output_nbest_file = os.path.join(model_dir, "nbest_predictions.json")
    output_null_log_odds_file = os.path.join(model_dir, "null_odds.json")

    all_predictions, all_nbest_json, scores_diff_json = write_predictions(
        eval_examples, eval_features, all_results,
        FLAGS.n_best_size, FLAGS.max_answer_length,
        FLAGS.do_lower_case, output_prediction_file,
        output_nbest_file, output_null_log_odds_file,
        FLAGS.version_2_with_negative,
        FLAGS.verbose_logging)

    if FLAGS.do_eval:
      if FLAGS.version_2_with_negative:
        tf.compat.v1.logging.fatal("Eval for v2 is not supported")
        pass
      else:
        with tf.io.gfile.GFile(FLAGS.predict_file, 'r') as reader:
          dataset_json = json.load(reader)
          pred_dataset = dataset_json['data']
        f1 = exact_match = total = 0
        for article in pred_dataset:
          for paragraph in article["paragraphs"]:
            for qa in paragraph["qas"]:
              total += 1
              if qa["id"] not in all_predictions:
                message = "Unanswered question " + qa["id"] + " will receive score 0."
                tf.compat.v1.logging.error(message)
                continue
              ground_truths = [entry["text"] for entry in qa["answers"]]
              prediction = all_predictions[qa["id"]]
              exact_match += _metric_max_over_ground_truths(_exact_match_score,
                                                            prediction, ground_truths)
              f1 += _metric_max_over_ground_truths(_f1_score, prediction,
                                                   ground_truths)

        exact_match = exact_match / total
        f1 = f1 / total
        tf.compat.v1.logging.warning("Exact matches %s" % str(exact_match))
        tf.compat.v1.logging.warning("Final accuracy %s" % str(f1))

        with TBSummary(os.path.join(model_dir, 'eval')) as summary_writer:
          summary_writer.add_scalar('accuracy', f1, 0)
          summary_writer.add_scalar('exact_matches', exact_match, 0)


def _metric_max_over_ground_truths(metric_fn, prediction, ground_truths):
  """Computes the max over all metric scores."""
  scores_for_ground_truths = []
  for ground_truth in ground_truths:
    score = metric_fn(prediction, ground_truth)
    scores_for_ground_truths.append(score)
  return max(scores_for_ground_truths)


def _exact_match_score(prediction, ground_truth):
  """Checks if predicted answer exactly matches ground truth answer."""
  return _normalize_answer(prediction) == _normalize_answer(ground_truth)


def _f1_score(prediction, ground_truth):
  """Computes F1 score by comparing prediction to ground truth."""
  prediction_tokens = _normalize_answer(prediction).split()
  ground_truth_tokens = _normalize_answer(ground_truth).split()
  prediction_counter = collections.Counter(prediction_tokens)
  ground_truth_counter = collections.Counter(ground_truth_tokens)
  common = prediction_counter & ground_truth_counter
  num_same = sum(common.values())
  if num_same == 0:
    return 0
  precision = 1.0 * num_same / len(prediction_tokens)
  recall = 1.0 * num_same / len(ground_truth_tokens)
  f1 = (2 * precision * recall) / (precision + recall)
  return f1


def _normalize_answer(s):
  """Lowers text and remove punctuation, articles and extra whitespace."""

  def remove_articles(text):
    import re
    return re.sub(r"\b(a|an|the)\b", " ", text)

  def white_space_fix(text):
    return " ".join(text.split())

  def remove_punc(text):
    import string
    exclude = set(string.punctuation)
    return "".join(ch for ch in text if ch not in exclude)

  def lower(text):
    return text.lower()

  return white_space_fix(remove_articles(remove_punc(lower(s))))


if __name__ == "__main__":
  init_squad_flags()

  tf.compat.v1.enable_resource_variables()

  flags.mark_flag_as_required("vocab_file")
  flags.mark_flag_as_required("distilbert_config_file")
  flags.mark_flag_as_required("output_dir")
  if FLAGS.deterministic_run:
    modeling.dropout = lambda input_tensor, dropout_prob: input_tensor  # disable dropout

  if FLAGS.bf16_config_path is None:
    os.environ.setdefault('HABANA_INITIAL_WORKSPACE_SIZE_MB', '13271')
  else:
    os.environ['TF_BF16_CONVERSION'] = FLAGS.bf16_config_path
    os.environ.setdefault('HABANA_INITIAL_WORKSPACE_SIZE_MB', '17371')
  os.environ.setdefault("TF_DISABLE_MKL", "1")

  bert_config = json.load(open(FLAGS.distilbert_config_file, 'r'))
  if 'hidden_size' in bert_config:
    if bert_config['hidden_size'] == 768:
      # cluster slicing optimization tested for bert base, works well with this size
      os.environ.setdefault('TF_PRELIMINARY_CLUSTER_SIZE', '1000')
    elif bert_config['hidden_size'] >= 1024:
      if FLAGS.bf16_config_path is None:
        os.environ.setdefault('HABANA_INITIAL_WORKSPACE_SIZE_MB', '17257')
      else:
        os.environ.setdefault('HABANA_INITIAL_WORKSPACE_SIZE_MB', '21393')

  if FLAGS.use_horovod:
    if hvd is None:
      raise RuntimeError(
          "Problem encountered during Horovod import. Please make sure that habana-horovod package is installed.")
    assert not FLAGS.cpu_only, "Horovod without HPU is not supported in helpers."
    if FLAGS.horovod_hierarchical_allreduce:
      os.environ['HOROVOD_HIERARCHICAL_ALLREDUCE'] = "1"
    hvd.init()

  if not FLAGS.cpu_only:
    load_habana_module()

  host_name = socket.gethostname()
  vmulti = os.environ.get('MULTI_HLS_IPS')
  v1 = os.environ.get('HABANA_INITIAL_WORKSPACE_SIZE_MB')
  v2 = os.environ.get('TF_BF16_CONVERSION')
  print(f"{host_name}: MULTI_HLS_IPS = {vmulti}")
  print(f"{host_name}: HABANA_INITIAL_WORKSPACE_SIZE_MB = {v1}")
  print(f"{host_name}: TF_BF16_CONVERSION = {v2}")
  sys.stdout.flush()
  sys.stderr.flush()

  tf.compat.v1.app.run()