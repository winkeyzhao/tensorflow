# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
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
#==============================================================================

"""Data Flow Operations."""
# pylint: disable=g-bad-name
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import hashlib
import re

import six

from tensorflow.python.framework import common_shapes
from tensorflow.python.framework import dtypes as _dtypes
from tensorflow.python.framework import ops
from tensorflow.python.framework import random_seed
from tensorflow.python.framework import tensor_shape
from tensorflow.python.framework import tensor_util
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import gen_data_flow_ops
from tensorflow.python.ops import math_ops
# go/tf-wildcard-import
# pylint: disable=wildcard-import
from tensorflow.python.ops.gen_data_flow_ops import *
# pylint: enable=wildcard-import


def _as_type_list(dtypes):
  """Convert dtypes to a list of types."""
  assert dtypes is not None
  if not (isinstance(dtypes, list) or isinstance(dtypes, tuple)):
    # We have a single type.
    return [dtypes]
  else:
    # We have a list or tuple of types.
    return list(dtypes)


def _as_shape_list(shapes, dtypes, unknown_dim_allowed=False,
                   unknown_rank_allowed=False):
  """Convert shapes to a list of tuples of int (or None)."""
  if unknown_dim_allowed:
    if (not isinstance(shapes, collections.Sequence)
        or not shapes
        or any(shape is None or isinstance(shape, int) for shape in shapes)):
      raise ValueError(
          "When providing partial shapes, a list of shapes must be provided.")
  if shapes is None: return None
  if isinstance(shapes, tensor_shape.TensorShape):
    shapes = [shapes]
  if not isinstance(shapes, (tuple, list)):
    raise TypeError(
        "shapes must be a TensorShape or a list or tuple of TensorShapes.")
  if all(shape is None or isinstance(shape, int) for shape in shapes):
    # We have a single shape.
    shapes = [shapes]
  shapes = [tensor_shape.as_shape(shape) for shape in shapes]
  if not unknown_dim_allowed:
    if any([not shape.is_fully_defined() for shape in shapes]):
      raise ValueError("All shapes must be fully defined: %s" % shapes)
  if not unknown_rank_allowed:
    if any([shape.dims is None for shape in shapes]):
      raise ValueError("All shapes must have a defined rank: %s" % shapes)

  return shapes


def _as_name_list(names, dtypes):
  if names is None:
    return None
  if not isinstance(names, (list, tuple)):
    names = [names]
  if len(names) != len(dtypes):
    raise ValueError("List of names must have the same length as the list "
                     "of dtypes")
  return list(names)


def _shape_common(s1, s2):
  """The greatest lower bound (ordered by specificity) TensorShape."""
  s1 = tensor_shape.TensorShape(s1)
  s2 = tensor_shape.TensorShape(s2)
  if s1.ndims is None or s2.ndims is None or s1.ndims != s2.ndims:
    return tensor_shape.unknown_shape()
  d = [
      d1 if d1 is not None and d1 == d2 else None
      for (d1, d2) in zip(s1.as_list(), s2.as_list())]
  return tensor_shape.TensorShape(d)


# pylint: disable=protected-access
class QueueBase(object):
  """Base class for queue implementations.

  A queue is a TensorFlow data structure that stores tensors across
  multiple steps, and exposes operations that enqueue and dequeue
  tensors.

  Each queue element is a tuple of one or more tensors, where each
  tuple component has a static dtype, and may have a static shape. The
  queue implementations support versions of enqueue and dequeue that
  handle single elements, versions that support enqueuing and
  dequeuing a batch of elements at once.

  See [`tf.FIFOQueue`](#FIFOQueue) and
  [`tf.RandomShuffleQueue`](#RandomShuffleQueue) for concrete
  implementations of this class, and instructions on how to create
  them.

  @@enqueue
  @@enqueue_many

  @@dequeue
  @@dequeue_many

  @@size

  @@close

  """

  def __init__(self, dtypes, shapes, names, queue_ref):
    """Constructs a queue object from a queue reference.

    The two optional lists, `shapes` and `names`, must be of the same length
    as `dtypes` if provided.  The values at a given index `i` indicate the
    shape and name to use for the corresponding queue component in `dtypes`.

    Args:
      dtypes:  A list of types.  The length of dtypes must equal the number
        of tensors in each element.
      shapes: Constraints on the shapes of tensors in an element:
        A list of shape tuples or None. This list is the same length
        as dtypes.  If the shape of any tensors in the element are constrained,
        all must be; shapes can be None if the shapes should not be constrained.
      names: Optional list of names.  If provided, the `enqueue()` and
        `dequeue()` methods will use dictionaries with these names as keys.
        Must be None or a list or tuple of the same length as `dtypes`.
      queue_ref: The queue reference, i.e. the output of the queue op.

    Raises:
      ValueError: If one of the arguments is invalid.
    """
    self._dtypes = dtypes
    if shapes is not None:
      if len(shapes) != len(dtypes):
        raise ValueError("Queue shapes must have the same length as dtypes")
      self._shapes = [tensor_shape.TensorShape(s) for s in shapes]
    else:
      self._shapes = [tensor_shape.unknown_shape() for _ in self._dtypes]
    if names is not None:
      if len(names) != len(dtypes):
        raise ValueError("Queue names must have the same length as dtypes")
      self._names = names
    else:
      self._names = None
    self._queue_ref = queue_ref
    self._name = self._queue_ref.op.name.split("/")[-1]

  @staticmethod
  def from_list(index, queues):
    """Create a queue using the queue reference from `queues[index]`.

    Args:
      index: An integer scalar tensor that determines the input that gets
        selected.
      queues: A list of `QueueBase` objects.

    Returns:
      A `QueueBase` object.

    Raises:
      TypeError: When `queues` is not a list of `QueueBase` objects,
        or when the data types of `queues` are not all the same.
    """
    if ((not queues) or
        (not isinstance(queues, list)) or
        (not all(isinstance(x, QueueBase) for x in queues))):
      raise TypeError("A list of queues expected")

    dtypes = queues[0].dtypes
    if not all([dtypes == q.dtypes for q in queues[1:]]):
      raise TypeError("Queues do not have matching component dtypes.")

    names = queues[0].names
    if not all([names == q.names for q in queues[1:]]):
      raise TypeError("Queues do not have matching component names.")

    queue_shapes = [q.shapes for q in queues]
    reduced_shapes = [
        six.moves.reduce(_shape_common, s) for s in zip(*queue_shapes)]

    queue_refs = [x.queue_ref for x in queues]
    selected_queue = control_flow_ops.ref_select(index, queue_refs)
    return QueueBase(dtypes=dtypes, shapes=reduced_shapes, names=names,
                     queue_ref=selected_queue)

  @property
  def queue_ref(self):
    """The underlying queue reference."""
    return self._queue_ref

  @property
  def name(self):
    """The name of the underlying queue."""
    return self._queue_ref.op.name

  @property
  def dtypes(self):
    """The list of dtypes for each component of a queue element."""
    return self._dtypes

  @property
  def shapes(self):
    """The list of shapes for each component of a queue element."""
    return self._shapes

  @property
  def names(self):
    """The list of names for each component of a queue element."""
    return self._names

  def _check_enqueue_dtypes(self, vals):
    """Validate and convert `vals` to a list of `Tensor`s.

    The `vals` argument can be a Tensor, a list or tuple of tensors, or a
    dictionary with tensor values.

    If it is a dictionary, the queue must have been constructed with a
    `names` attribute and the dictionary keys must math the queue names.
    If the queue was constructed with a `names` attribute, `vals` must
    be a dictionary.

    Args:
      vals: A tensor, a list or tuple of tensors, or a dictionary..

    Returns:
      A list of `Tensor` objects.

    Raises:
      ValueError: If `vals` is invalid.
    """
    if isinstance(vals, dict):
      if not self._names:
        raise ValueError("Queue must have names to enqueue a dictionary")
      if sorted(self._names) != sorted(vals.keys()):
        raise ValueError("Keys in dictionary to enqueue do not match "
                         "names of Queue.  Dictionary: (%s), Queue: (%s)" %
                         (sorted(vals.keys()), sorted(self._names)))
      # The order of values in `self._names` indicates the order in which the
      # tensors in the dictionary `vals` must be listed.
      vals = [vals[k] for k in self._names]
    else:
      if self._names:
        raise ValueError("You must enqueue a dictionary in a Queue with names")
      if not isinstance(vals, (list, tuple)):
        vals = [vals]

    tensors = []
    for i, (val, dtype) in enumerate(zip(vals, self._dtypes)):
      tensors.append(ops.convert_to_tensor(val, dtype=dtype,
                                           name="component_%d" % i))

    return tensors

  def _scope_vals(self, vals):
    """Return a list of values to pass to `name_scope()`.

    Args:
      vals: A tensor, a list or tuple of tensors, or a dictionary.

    Returns:
      The values in vals as a list.
    """
    if isinstance(vals, (list, tuple)):
      return vals
    elif isinstance(vals, dict):
      return vals.values()
    else:
      return [vals]

  def enqueue(self, vals, name=None):
    """Enqueues one element to this queue.

    If the queue is full when this operation executes, it will block
    until the element has been enqueued.

    At runtime, this operation may raise an error if the queue is
    [closed](#QueueBase.close) before or during its execution. If the
    queue is closed before this operation runs,
    `tf.errors.CancelledError` will be raised. If this operation is
    blocked, and either (i) the queue is closed by a close operation
    with `cancel_pending_enqueues=True`, or (ii) the session is
    [closed](../../api_docs/python/client.md#Session.close),
    `tf.errors.CancelledError` will be raised.

    Args:
      vals: A tensor, a list or tuple of tensors, or a dictionary containing
        the values to enqueue.
      name: A name for the operation (optional).

    Returns:
      The operation that enqueues a new tuple of tensors to the queue.
    """
    with ops.name_scope(name, "%s_enqueue" % self._name,
                        self._scope_vals(vals)) as scope:
      vals = self._check_enqueue_dtypes(vals)

      # NOTE(mrry): Not using a shape function because we need access to
      # the `QueueBase` object.
      for val, shape in zip(vals, self._shapes):
        val.get_shape().assert_is_compatible_with(shape)

      return gen_data_flow_ops._queue_enqueue(self._queue_ref, vals, name=scope)

  def enqueue_many(self, vals, name=None):
    """Enqueues zero or more elements to this queue.

    This operation slices each component tensor along the 0th dimension to
    make multiple queue elements. All of the tensors in `vals` must have the
    same size in the 0th dimension.

    If the queue is full when this operation executes, it will block
    until all of the elements have been enqueued.

    At runtime, this operation may raise an error if the queue is
    [closed](#QueueBase.close) before or during its execution. If the
    queue is closed before this operation runs,
    `tf.errors.CancelledError` will be raised. If this operation is
    blocked, and either (i) the queue is closed by a close operation
    with `cancel_pending_enqueues=True`, or (ii) the session is
    [closed](../../api_docs/python/client.md#Session.close),
    `tf.errors.CancelledError` will be raised.

    Args:
      vals: A tensor, a list or tuple of tensors, or a dictionary
        from which the queue elements are taken.
      name: A name for the operation (optional).

    Returns:
      The operation that enqueues a batch of tuples of tensors to the queue.
    """
    with ops.name_scope(name, "%s_EnqueueMany" % self._name,
                        self._scope_vals(vals)) as scope:
      vals = self._check_enqueue_dtypes(vals)

      # NOTE(mrry): Not using a shape function because we need access to
      # the `QueueBase` object.
      batch_dim = vals[0].get_shape().with_rank_at_least(1)[0]
      for val, shape in zip(vals, self._shapes):
        batch_dim = batch_dim.merge_with(
            val.get_shape().with_rank_at_least(1)[0])
        val.get_shape()[1:].assert_is_compatible_with(shape)

      return gen_data_flow_ops._queue_enqueue_many(
          self._queue_ref, vals, name=scope)

  def _dequeue_return_value(self, tensors):
    """Return the value to return from a dequeue op.

    If the queue has names, return a dictionary with the
    names as keys.  Otherwise return either a single tensor
    or a list of tensors depending on the length of `tensors`.

    Args:
      tensors: List of tensors from the dequeue op.

    Returns:
      A single tensor, a list of tensors, or a dictionary
      of tensors.
    """
    if self._names:
      # The returned values in `tensors` are in the same order as
      # the names in `self._names`.
      return {n: tensors[i] for i, n in enumerate(self._names)}
    elif len(tensors) == 1:
      return tensors[0]
    else:
      return tensors

  def dequeue(self, name=None):
    """Dequeues one element from this queue.

    If the queue is empty when this operation executes, it will block
    until there is an element to dequeue.

    At runtime, this operation may raise an error if the queue is
    [closed](#QueueBase.close) before or during its execution. If the
    queue is closed, the queue is empty, and there are no pending
    enqueue operations that can fulfill this request,
    `tf.errors.OutOfRangeError` will be raised. If the session is
    [closed](../../api_docs/python/client.md#Session.close),
    `tf.errors.CancelledError` will be raised.

    Args:
      name: A name for the operation (optional).

    Returns:
      The tuple of tensors that was dequeued.
    """
    if name is None:
      name = "%s_Dequeue" % self._name
    ret = gen_data_flow_ops._queue_dequeue(
        self._queue_ref, self._dtypes, name=name)

    # NOTE(mrry): Not using a shape function because we need access to
    # the `QueueBase` object.
    op = ret[0].op
    for output, shape in zip(op.values(), self._shapes):
      output.set_shape(shape)

    return self._dequeue_return_value(ret)

  def dequeue_many(self, n, name=None):
    """Dequeues and concatenates `n` elements from this queue.

    This operation concatenates queue-element component tensors along
    the 0th dimension to make a single component tensor.  All of the
    components in the dequeued tuple will have size `n` in the 0th dimension.

    If the queue is closed and there are less than `n` elements left, then an
    `OutOfRange` exception is raised.

    At runtime, this operation may raise an error if the queue is
    [closed](#QueueBase.close) before or during its execution. If the
    queue is closed, the queue contains fewer than `n` elements, and
    there are no pending enqueue operations that can fulfill this
    request, `tf.errors.OutOfRangeError` will be raised. If the
    session is [closed](../../api_docs/python/client.md#Session.close),
    `tf.errors.CancelledError` will be raised.

    Args:
      n: A scalar `Tensor` containing the number of elements to dequeue.
      name: A name for the operation (optional).

    Returns:
      The tuple of concatenated tensors that was dequeued.
    """
    if name is None:
      name = "%s_DequeueMany" % self._name

    ret = gen_data_flow_ops._queue_dequeue_many(
        self._queue_ref, n=n, component_types=self._dtypes, name=name)

    # NOTE(mrry): Not using a shape function because we need access to
    # the Queue object.
    op = ret[0].op
    batch_dim = tensor_shape.Dimension(tensor_util.constant_value(op.inputs[1]))
    for output, shape in zip(op.values(), self._shapes):
      output.set_shape(tensor_shape.TensorShape([batch_dim]).concatenate(shape))

    return self._dequeue_return_value(ret)

  def dequeue_up_to(self, n, name=None):
    """Dequeues and concatenates `n` elements from this queue.

    **Note** This operation is not supported by all queues.  If a queue does not
    support DequeueUpTo, then a `tf.errors.UnimplementedError` is raised.

    This operation concatenates queue-element component tensors along
    the 0th dimension to make a single component tensor. If the queue
    has not been closed, all of the components in the dequeued tuple
    will have size `n` in the 0th dimension.

    If the queue is closed and there are more than `0` but fewer than
    `n` elements remaining, then instead of raising a
    `tf.errors.OutOfRangeError` like [`dequeue_many`](#QueueBase.dequeue_many),
    less than `n` elements are returned immediately.  If the queue is
    closed and there are `0` elements left in the queue, then a
    `tf.errors.OutOfRangeError` is raised just like in `dequeue_many`.
    Otherwise the behavior is identical to `dequeue_many`.

    Args:
      n: A scalar `Tensor` containing the number of elements to dequeue.
      name: A name for the operation (optional).

    Returns:
      The tuple of concatenated tensors that was dequeued.
    """
    if name is None:
      name = "%s_DequeueUpTo" % self._name

    ret = gen_data_flow_ops._queue_dequeue_up_to(
        self._queue_ref, n=n, component_types=self._dtypes, name=name)

    # NOTE(mrry): Not using a shape function because we need access to
    # the Queue object.
    op = ret[0].op
    for output, shape in zip(op.values(), self._shapes):
      output.set_shape(tensor_shape.TensorShape([None]).concatenate(shape))

    return self._dequeue_return_value(ret)

  def close(self, cancel_pending_enqueues=False, name=None):
    """Closes this queue.

    This operation signals that no more elements will be enqueued in
    the given queue. Subsequent `enqueue` and `enqueue_many`
    operations will fail. Subsequent `dequeue` and `dequeue_many`
    operations will continue to succeed if sufficient elements remain
    in the queue. Subsequent `dequeue` and `dequeue_many` operations
    that would block will fail immediately.

    If `cancel_pending_enqueues` is `True`, all pending requests will also
    be cancelled.

    Args:
      cancel_pending_enqueues: (Optional.) A boolean, defaulting to
        `False` (described above).
      name: A name for the operation (optional).

    Returns:
      The operation that closes the queue.
    """
    if name is None:
      name = "%s_Close" % self._name
    return gen_data_flow_ops._queue_close(
        self._queue_ref, cancel_pending_enqueues=cancel_pending_enqueues,
        name=name)

  def size(self, name=None):
    """Compute the number of elements in this queue.

    Args:
      name: A name for the operation (optional).

    Returns:
      A scalar tensor containing the number of elements in this queue.
    """
    if name is None:
      name = "%s_Size" % self._name
    return gen_data_flow_ops._queue_size(self._queue_ref, name=name)


class RandomShuffleQueue(QueueBase):
  """A queue implementation that dequeues elements in a random order.

  See [`tf.QueueBase`](#QueueBase) for a description of the methods on
  this class.

  @@__init__
  """

  def __init__(self, capacity, min_after_dequeue, dtypes, shapes=None,
               names=None, seed=None, shared_name=None,
               name="random_shuffle_queue"):
    """Create a queue that dequeues elements in a random order.

    A `RandomShuffleQueue` has bounded capacity; supports multiple
    concurrent producers and consumers; and provides exactly-once
    delivery.

    A `RandomShuffleQueue` holds a list of up to `capacity`
    elements. Each element is a fixed-length tuple of tensors whose
    dtypes are described by `dtypes`, and whose shapes are optionally
    described by the `shapes` argument.

    If the `shapes` argument is specified, each component of a queue
    element must have the respective fixed shape. If it is
    unspecified, different queue elements may have different shapes,
    but the use of `dequeue_many` is disallowed.

    The `min_after_dequeue` argument allows the caller to specify a
    minimum number of elements that will remain in the queue after a
    `dequeue` or `dequeue_many` operation completes, to ensure a
    minimum level of mixing of elements. This invariant is maintained
    by blocking those operations until sufficient elements have been
    enqueued. The `min_after_dequeue` argument is ignored after the
    queue has been closed.

    Args:
      capacity: An integer. The upper bound on the number of elements
        that may be stored in this queue.
      min_after_dequeue: An integer (described above).
      dtypes:  A list of `DType` objects. The length of `dtypes` must equal
        the number of tensors in each queue element.
      shapes: (Optional.) A list of fully-defined `TensorShape` objects
        with the same length as `dtypes`, or `None`.
      names: (Optional.) A list of string naming the components in the queue
        with the same length as `dtypes`, or `None`.  If specified the dequeue
        methods return a dictionary with the names as keys.
      seed: A Python integer. Used to create a random seed. See
        [`set_random_seed`](../../api_docs/python/constant_op.md#set_random_seed)
        for behavior.
      shared_name: (Optional.) If non-empty, this queue will be shared under
        the given name across multiple sessions.
      name: Optional name for the queue operation.
    """
    dtypes = _as_type_list(dtypes)
    shapes = _as_shape_list(shapes, dtypes)
    names = _as_name_list(names, dtypes)
    seed1, seed2 = random_seed.get_seed(seed)
    if seed1 is None and seed2 is None:
      seed1, seed2 = 0, 0
    elif seed is None and shared_name is not None:
      # This means that graph seed is provided but op seed is not provided.
      # If shared_name is also provided, make seed2 depend only on the graph
      # seed and shared_name. (seed2 from get_seed() is generally dependent on
      # the id of the last op created.)
      string = (str(seed1) + shared_name).encode("utf-8")
      seed2 = int(hashlib.md5(string).hexdigest()[:8], 16) & 0x7FFFFFFF
    queue_ref = gen_data_flow_ops._random_shuffle_queue(
        component_types=dtypes, shapes=shapes, capacity=capacity,
        min_after_dequeue=min_after_dequeue, seed=seed1, seed2=seed2,
        shared_name=shared_name, name=name)

    super(RandomShuffleQueue, self).__init__(dtypes, shapes, names, queue_ref)


class FIFOQueue(QueueBase):
  """A queue implementation that dequeues elements in first-in first-out order.

  See [`tf.QueueBase`](#QueueBase) for a description of the methods on
  this class.

  @@__init__
  """

  def __init__(self, capacity, dtypes, shapes=None, names=None,
               shared_name=None, name="fifo_queue"):
    """Creates a queue that dequeues elements in a first-in first-out order.

    A `FIFOQueue` has bounded capacity; supports multiple concurrent
    producers and consumers; and provides exactly-once delivery.

    A `FIFOQueue` holds a list of up to `capacity` elements. Each
    element is a fixed-length tuple of tensors whose dtypes are
    described by `dtypes`, and whose shapes are optionally described
    by the `shapes` argument.

    If the `shapes` argument is specified, each component of a queue
    element must have the respective fixed shape. If it is
    unspecified, different queue elements may have different shapes,
    but the use of `dequeue_many` is disallowed.

    Args:
      capacity: An integer. The upper bound on the number of elements
        that may be stored in this queue.
      dtypes:  A list of `DType` objects. The length of `dtypes` must equal
        the number of tensors in each queue element.
      shapes: (Optional.) A list of fully-defined `TensorShape` objects
        with the same length as `dtypes`, or `None`.
      names: (Optional.) A list of string naming the components in the queue
        with the same length as `dtypes`, or `None`.  If specified the dequeue
        methods return a dictionary with the names as keys.
      shared_name: (Optional.) If non-empty, this queue will be shared under
        the given name across multiple sessions.
      name: Optional name for the queue operation.
    """
    dtypes = _as_type_list(dtypes)
    shapes = _as_shape_list(shapes, dtypes)
    names = _as_name_list(names, dtypes)
    queue_ref = gen_data_flow_ops._fifo_queue(
        component_types=dtypes, shapes=shapes, capacity=capacity,
        shared_name=shared_name, name=name)

    super(FIFOQueue, self).__init__(dtypes, shapes, names, queue_ref)


class PaddingFIFOQueue(QueueBase):
  """A FIFOQueue that supports batching variable-sized tensors by padding.

  A `PaddingFIFOQueue` may contain components with dynamic shape, while also
  supporting `dequeue_many`.  See the constructor for more details.

  See [`tf.QueueBase`](#QueueBase) for a description of the methods on
  this class.

  @@__init__
  """

  def __init__(self, capacity, dtypes, shapes, names=None, shared_name=None,
               name="padding_fifo_queue"):
    """Creates a queue that dequeues elements in a first-in first-out order.

    A `PaddingFIFOQueue` has bounded capacity; supports multiple concurrent
    producers and consumers; and provides exactly-once delivery.

    A `PaddingFIFOQueue` holds a list of up to `capacity` elements. Each
    element is a fixed-length tuple of tensors whose dtypes are
    described by `dtypes`, and whose shapes are described by the `shapes`
    argument.

    The `shapes` argument must be specified; each component of a queue
    element must have the respective shape.  Shapes of fixed
    rank but variable size are allowed by setting any shape dimension to None.
    In this case, the inputs' shape may vary along the given dimension, and
    `dequeue_many` will pad the given dimension with zeros up to the maximum
    shape of all elements in the given batch.

    Args:
      capacity: An integer. The upper bound on the number of elements
        that may be stored in this queue.
      dtypes:  A list of `DType` objects. The length of `dtypes` must equal
        the number of tensors in each queue element.
      shapes: A list of `TensorShape` objects, with the same length as
        `dtypes`.  Any dimension in the `TensorShape` containing value
        `None` is dynamic and allows values to be enqueued with
         variable size in that dimension.
      names: (Optional.) A list of string naming the components in the queue
        with the same length as `dtypes`, or `None`.  If specified the dequeue
        methods return a dictionary with the names as keys.
      shared_name: (Optional.) If non-empty, this queue will be shared under
        the given name across multiple sessions.
      name: Optional name for the queue operation.

    Raises:
      ValueError: If shapes is not a list of shapes, or the lengths of dtypes
        and shapes do not match, or if names is specified and the lengths of
        dtypes and names do not match.
    """
    dtypes = _as_type_list(dtypes)
    shapes = _as_shape_list(shapes, dtypes, unknown_dim_allowed=True)
    names = _as_name_list(names, dtypes)
    if len(dtypes) != len(shapes):
      raise ValueError("Shapes must be provided for all components, "
                       "but received %d dtypes and %d shapes."
                       % (len(dtypes), len(shapes)))

    queue_ref = gen_data_flow_ops._padding_fifo_queue(
        component_types=dtypes, shapes=shapes, capacity=capacity,
        shared_name=shared_name, name=name)

    super(PaddingFIFOQueue, self).__init__(dtypes, shapes, names, queue_ref)


class PriorityQueue(QueueBase):
  """A queue implementation that dequeues elements in prioritized order.

  See [`tf.QueueBase`](#QueueBase) for a description of the methods on
  this class.

  @@__init__
  """

  def __init__(self, capacity, types, shapes=None, names=None, shared_name=None,
               name="priority_queue"):
    """Creates a queue that dequeues elements in a first-in first-out order.

    A `PriorityQueue` has bounded capacity; supports multiple concurrent
    producers and consumers; and provides exactly-once delivery.

    A `PriorityQueue` holds a list of up to `capacity` elements. Each
    element is a fixed-length tuple of tensors whose dtypes are
    described by `types`, and whose shapes are optionally described
    by the `shapes` argument.

    If the `shapes` argument is specified, each component of a queue
    element must have the respective fixed shape. If it is
    unspecified, different queue elements may have different shapes,
    but the use of `dequeue_many` is disallowed.

    Enqueues and Dequeues to the `PriorityQueue` must include an additional
    tuple entry at the beginning: the `priority`.  The priority must be
    an int64 scalar (for `enqueue`) or an int64 vector (for `enqueue_many`).

    Args:
      capacity: An integer. The upper bound on the number of elements
        that may be stored in this queue.
      types:  A list of `DType` objects. The length of `types` must equal
        the number of tensors in each queue element, except the first priority
        element.  The first tensor in each element is the priority,
        which must be type int64.
      shapes: (Optional.) A list of fully-defined `TensorShape` objects,
        with the same length as `types`, or `None`.
      names: (Optional.) A list of strings naming the components in the queue
        with the same length as `dtypes`, or `None`.  If specified, the dequeue
        methods return a dictionary with the names as keys.
      shared_name: (Optional.) If non-empty, this queue will be shared under
        the given name across multiple sessions.
      name: Optional name for the queue operation.
    """
    types = _as_type_list(types)
    shapes = _as_shape_list(shapes, types)

    queue_ref = gen_data_flow_ops._priority_queue(
        component_types=types, shapes=shapes, capacity=capacity,
        shared_name=shared_name, name=name)

    priority_dtypes = [_dtypes.int64] + types
    priority_shapes = [()] + shapes if shapes else shapes

    super(PriorityQueue, self).__init__(
        priority_dtypes, priority_shapes, names, queue_ref)


# TODO(josh11b): class BatchQueue(QueueBase):


class Barrier(object):
  """Represents a key-value map that persists across graph executions."""

  def __init__(self, types, shapes=None, shared_name=None, name="barrier"):
    """Creates a barrier that persists across different graph executions.

    A barrier represents a key-value map, where each key is a string, and
    each value is a tuple of tensors.

    At runtime, the barrier contains 'complete' and 'incomplete'
    elements. A complete element has defined tensors for all
    components of its value tuple, and may be accessed using
    take_many. An incomplete element has some undefined components in
    its value tuple, and may be updated using insert_many.

    The barrier call `take_many` outputs values in a particular order.
    First, it only outputs completed values.  Second, the order in which
    completed values are returned matches the order in which their very
    first component was inserted into the barrier.  So, for example, for this
    sequence of insertions and removals:

      barrier = Barrier((tf.string, tf.int32), shapes=((), ()))
      barrier.insert_many(0, keys=["k1", "k2"], values=["a", "b"]).run()
      barrier.insert_many(1, keys=["k1"], values=[1]).run()
      barrier.insert_many(0, keys=["k3"], values=["c"]).run()
      barrier.insert_many(1, keys=["k3"], values=[3]).run()
      barrier.insert_many(1, keys=["k2"], values=[2]).run()

      (indices, keys, values) = barrier.take_many(2)
      (indices_val, keys_val, values0_val, values1_val) =
         session.run([indices, keys, values[0], values[1]])

    The output will be (up to permutation of "k1" and "k2"):

      indices_val == (-2**63, -2**63)
      keys_val == ("k1", "k2")
      values0_val == ("a", "b")
      values1_val == (1, 2)

    Note the key "k2" was inserted into the barrier before "k3".  Even though
    "k3" was completed first, both are complete by the time
    take_many is called.  As a result, "k2" is prioritized and "k1" and "k2"
    are returned first.  "k3" remains in the barrier until the next execution
    of `take_many`.  Since "k1" and "k2" had their first insertions into
    the barrier together, their indices are the same (-2**63).  The index
    of "k3" will be -2**63 + 1, because it was the next new inserted key.

    Args:
      types: A single dtype or a tuple of dtypes, corresponding to the
        dtypes of the tensor elements that comprise a value in this barrier.
      shapes: Optional. Constraints on the shapes of tensors in the values:
        a single tensor shape tuple; a tuple of tensor shape tuples
        for each barrier-element tuple component; or None if the shape should
        not be constrained.
      shared_name: Optional. If non-empty, this barrier will be shared under
        the given name across multiple sessions.
      name: Optional name for the barrier op.

    Raises:
      ValueError: If one of the `shapes` indicate no elements.
    """
    self._types = _as_type_list(types)

    if shapes is not None:
      shapes = _as_shape_list(shapes, self._types)
      self._shapes = [tensor_shape.TensorShape(s) for s in shapes]
      for i, shape in enumerate(self._shapes):
        if shape.num_elements() == 0:
          raise ValueError("Empty tensors are not supported, but received "
                           "shape '%s' at index %d" % (shape, i))
    else:
      self._shapes = [tensor_shape.unknown_shape() for _ in self._types]

    self._barrier_ref = gen_data_flow_ops._barrier(
        component_types=self._types, shapes=self._shapes,
        shared_name=shared_name, name=name)
    self._name = self._barrier_ref.op.name.split("/")[-1]

  @property
  def barrier_ref(self):
    """Get the underlying barrier reference."""
    return self._barrier_ref

  @property
  def name(self):
    """The name of the underlying barrier."""
    return self._barrier_ref.op.name

  def insert_many(self, component_index, keys, values, name=None):
    """For each key, assigns the respective value to the specified component.

    This operation updates each element at component_index.

    Args:
      component_index: The component of the value that is being assigned.
      keys: A vector of keys, with length n.
      values: An any-dimensional tensor of values, which are associated with the
        respective keys. The first dimension must have length n.
      name: Optional name for the op.

    Returns:
      The operation that performs the insertion.
    Raises:
      InvalidArgumentsError: If inserting keys and values without elements.
    """
    if name is None:
      name = "%s_BarrierInsertMany" % self._name
    return gen_data_flow_ops._barrier_insert_many(
        self._barrier_ref, keys, values, component_index, name=name)

  def take_many(self,
                num_elements,
                allow_small_batch=False,
                timeout=None,
                name=None):
    """Takes the given number of completed elements from this barrier.

    This operation concatenates completed-element component tensors along
    the 0th dimension to make a single component tensor.

    If barrier has no completed elements, this operation will block
    until there are 'num_elements' elements to take.

    Args:
      num_elements: The number of elements to take.
      allow_small_batch: If the barrier is closed, don't block if there are less
        completed elements than requested, but instead return all available
        completed elements.
        TODO(b/25743580): the semantics of `allow_small_batch` are experimental
        and may be extended to other cases in the future.
        TODO(ebrevdo): If a take_many(allow_small_batch=True) is blocking
        already when the barrier is closed, it will block for ever. Fix this
        by using asynchronous operations.
      timeout: This specifies the number of milliseconds to block
        before returning with DEADLINE_EXCEEDED. (This option is not
        supported yet.)
      name: A name for the operation (optional).

    Returns:
      A tuple of (index, key, value_list).
      "index" is a int64 tensor of length num_elements containing the
        index of the insert_many call for which the very first component of
        the given element was inserted into the Barrier, starting with
        the value -2**63.  Note, this value is different from the
        index of the insert_many call for which the element was completed.
      "key" is a string tensor of length num_elements containing the keys.
      "value_list" is a tuple of tensors, each one with size num_elements
        in the 0th dimension for each component in the barrier's values.

    """
    if name is None:
      name = "%s_BarrierTakeMany" % self._name
    ret = gen_data_flow_ops._barrier_take_many(self._barrier_ref,
                                               num_elements,
                                               self._types,
                                               allow_small_batch,
                                               timeout,
                                               name=name)

    # NOTE(mrry): Not using a shape function because we need access to
    # the Barrier object.
    op = ret[0].op
    if allow_small_batch:
      batch_dim = None
    else:
      batch_dim = tensor_shape.Dimension(
          tensor_util.constant_value(op.inputs[1]))
    op.outputs[0].set_shape(tensor_shape.vector(batch_dim))  # indices
    op.outputs[1].set_shape(tensor_shape.vector(batch_dim))  # keys
    for output, shape in zip(op.outputs[2:], self._shapes):  # value_list
      output.set_shape(tensor_shape.TensorShape([batch_dim]).concatenate(shape))

    return ret

  def close(self, cancel_pending_enqueues=False, name=None):
    """Closes this barrier.

    This operation signals that no more new key values will be inserted in the
    given barrier. Subsequent InsertMany operations with new keys will fail.
    InsertMany operations that just complement already existing keys with other
    components, will continue to succeed. Subsequent TakeMany operations will
    continue to succeed if sufficient elements remain in the barrier. Subsequent
    TakeMany operations that would block will fail immediately.

    If `cancel_pending_enqueues` is `True`, all pending requests to the
    underlying queue will also be cancelled, and completing of already
    started values is also not acceptable anymore.

    Args:
      cancel_pending_enqueues: (Optional.) A boolean, defaulting to
        `False` (described above).
      name: Optional name for the op.

    Returns:
      The operation that closes the barrier.
    """
    if name is None:
      name = "%s_BarrierClose" % self._name
    return gen_data_flow_ops._barrier_close(
        self._barrier_ref,
        cancel_pending_enqueues=cancel_pending_enqueues,
        name=name)

  def ready_size(self, name=None):
    """Compute the number of complete elements in the given barrier.

    Args:
      name: A name for the operation (optional).

    Returns:
      A single-element tensor containing the number of complete elements in the
      given barrier.
    """
    if name is None:
      name = "%s_BarrierReadySize" % self._name
    return gen_data_flow_ops._barrier_ready_size(self._barrier_ref, name=name)

  def incomplete_size(self, name=None):
    """Compute the number of incomplete elements in the given barrier.

    Args:
      name: A name for the operation (optional).

    Returns:
      A single-element tensor containing the number of incomplete elements in
      the given barrier.
    """
    if name is None:
      name = "%s_BarrierIncompleteSize" % self._name
    return gen_data_flow_ops._barrier_incomplete_size(
        self._barrier_ref, name=name)


def initialize_all_tables(name="init_all_tables"):
  """Returns an Op that initializes all tables of the default graph.

  Args:
    name: Optional name for the initialization op.

  Returns:
    An Op that initializes all tables.  Note that if there are
    not tables the returned Op is a NoOp.
  """
  initializers = ops.get_collection(ops.GraphKeys.TABLE_INITIALIZERS)
  if initializers:
    return control_flow_ops.group(*initializers, name=name)
  return control_flow_ops.no_op(name=name)


ops.NotDifferentiable("LookupTableFind")
ops.NotDifferentiable("LookupTableInsert")
ops.NotDifferentiable("LookupTableSize")
ops.NotDifferentiable("HashTable")
ops.NotDifferentiable("InitializeTable")
ops.NotDifferentiable("InitializeTableFromTextFile")
ops.NotDifferentiable("MutableDenseHashTable")
ops.NotDifferentiable("MutableHashTable")
ops.NotDifferentiable("MutableHashTableOfTensors")


ops.RegisterShape("QueueSize")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("Queue")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("FIFOQueue")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("PaddingFIFOQueue")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("RandomShuffleQueue")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("PriorityQueue")(common_shapes.call_cpp_shape_fn)


# NOTE(mrry): The following ops use higher-level information in the
# Queue class to provide shape information.
ops.RegisterShape("QueueDequeue")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("QueueDequeueMany")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("QueueDequeueUpTo")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("QueueEnqueue")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("QueueEnqueueMany")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("QueueClose")(common_shapes.call_cpp_shape_fn)

ops.RegisterShape("Stack")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("StackPush")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("StackPop")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("StackClose")(common_shapes.call_cpp_shape_fn)

# NOTE(mrry): Uses higher-level information in the Barrier class to
# provide shape information.
ops.RegisterShape("BarrierReadySize")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("BarrierIncompleteSize")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("Barrier")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("BarrierTakeMany")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("BarrierClose")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("BarrierInsertMany")(common_shapes.call_cpp_shape_fn)

ops.RegisterShape("GetSessionHandle")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("GetSessionTensor")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("DeleteSessionTensor")(common_shapes.call_cpp_shape_fn)


@ops.RegisterShape("DynamicPartition")
def _DynamicPartitionShape(op):
  """Shape function for data_flow_ops.dynamic_partition."""
  data_shape = op.inputs[0].get_shape()
  partitions_shape = op.inputs[1].get_shape()
  # If we don't know the rank of partitions, we don't know anything
  mid = partitions_shape.ndims
  if mid is None:
    result_shape = tensor_shape.unknown_shape()
  else:
    # data_shape must start with partitions_shape
    partitions_shape.assert_is_compatible_with(data_shape[:mid])
    # The partition shape is dynamic in the 0th dimension, and matches
    # data_shape in the remaining dimensions.
    result_shape = tensor_shape.TensorShape([None]).concatenate(
        data_shape[mid:])
  return [result_shape] * op.get_attr("num_partitions")


@ops.RegisterShape("DynamicStitch")
def _DynamicStitchShape(op):
  """Shape function for data_flow_ops.dynamic_stitch."""
  num_partitions = op.get_attr("N")
  indices_shapes = [t.get_shape() for t in op.inputs[0:num_partitions]]
  data_shapes = [t.get_shape() for t in op.inputs[num_partitions:]]
  output_shape = tensor_shape.unknown_shape()
  extra_shape = tensor_shape.TensorShape(None)
  for indices_shape, data_shape in zip(indices_shapes, data_shapes):
    indices_ndims = indices_shape.ndims
    if indices_ndims is not None:
      # Assert that data_shape starts with indices_shape
      indices_shape.merge_with(data_shape[:indices_ndims])
      # The rest belongs to output
      extra_shape = extra_shape.merge_with(data_shape[indices_ndims:])
  return [tensor_shape.TensorShape([None]).concatenate(extra_shape)]


ops.RegisterShape("LookupTableFind")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("LookupTableInsert")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("LookupTableImport")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("LookupTableSize")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("LookupTableExport")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("HashTable")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("MutableDenseHashTable")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("MutableHashTable")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("MutableHashTableOfTensors")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("InitializeTable")(common_shapes.call_cpp_shape_fn)
ops.RegisterShape("InitializeTableFromTextFile")(
    common_shapes.call_cpp_shape_fn)


class ConditionalAccumulatorBase(object):
  """A conditional accumulator for aggregating gradients.

  Up-to-date gradients (i.e., time step at which gradient was computed is
  equal to the accumulator's time step) are added to the accumulator.

  Extraction of the average gradient is blocked until the required number of
  gradients has been accumulated.
  """

  def __init__(self, dtype, shape, accumulator_ref):
    """Creates a new ConditionalAccumulator.

    Args:
      dtype: Datatype of the accumulated gradients.
      shape: Shape of the accumulated gradients.
      accumulator_ref: A handle to the conditional accumulator, created by sub-
        classes
    """
    self._dtype = dtype
    if shape is not None:
      self._shape = tensor_shape.TensorShape(shape)
    else:
      self._shape = tensor_shape.unknown_shape()
    self._accumulator_ref = accumulator_ref
    self._name = self._accumulator_ref.op.name.split("/")[-1]

  @property
  def accumulator_ref(self):
    """The underlying accumulator reference."""
    return self._accumulator_ref

  @property
  def name(self):
    """The name of the underlying accumulator."""
    return self._name

  @property
  def dtype(self):
    """The datatype of the gradients accumulated by this accumulator."""
    return self._dtype

  def num_accumulated(self, name=None):
    """Number of gradients that have currently been aggregated in accumulator.

    Args:
      name: Optional name for the operation.

    Returns:
      Number of accumulated gradients currently in accumulator.
    """
    if name is None:
      name = "%s_NumAccumulated" % self._name
    return gen_data_flow_ops.accumulator_num_accumulated(
        self._accumulator_ref, name=name)

  def set_global_step(self, new_global_step, name=None):
    """Sets the global time step of the accumulator.

    The operation logs a warning if we attempt to set to a time step that is
    lower than the accumulator's own time step.

    Args:
      new_global_step: Value of new time step. Can be a variable or a constant
      name: Optional name for the operation.

    Returns:
      Operation that sets the accumulator's time step.
    """
    return gen_data_flow_ops.accumulator_set_global_step(
        self._accumulator_ref,
        math_ops.to_int64(ops.convert_to_tensor(new_global_step)),
        name=name)


class ConditionalAccumulator(ConditionalAccumulatorBase):
  """A conditional accumulator for aggregating gradients.

  Up-to-date gradients (i.e., time step at which gradient was computed is
  equal to the accumulator's time step) are added to the accumulator.

  Extraction of the average gradient is blocked until the required number of
  gradients has been accumulated.
  """

  def __init__(self,
               dtype,
               shape=None,
               shared_name=None,
               name="conditional_accumulator"):
    """Creates a new ConditionalAccumulator.

    Args:
      dtype: Datatype of the accumulated gradients.
      shape: Shape of the accumulated gradients.
      shared_name: Optional. If non-empty, this accumulator will be shared under
        the given name across multiple sessions.
      name: Optional name for the accumulator.
    """
    accumulator_ref = gen_data_flow_ops.conditional_accumulator(
        dtype=dtype, shape=shape, shared_name=shared_name, name=name)
    super(ConditionalAccumulator, self).__init__(dtype, shape, accumulator_ref)

  def apply_grad(self, grad, local_step=0, name=None):
    """Attempts to apply a gradient to the accumulator.

    The attempt is silently dropped if the gradient is stale, i.e., local_step
    is less than the accumulator's global time step.

    Args:
      grad: The gradient tensor to be applied.
      local_step: Time step at which the gradient was computed.
      name: Optional name for the operation.

    Returns:
      The operation that (conditionally) applies a gradient to the accumulator.

    Raises:
      ValueError: If grad is of the wrong shape
    """
    grad = ops.convert_to_tensor(grad, self._dtype)
    grad.get_shape().assert_is_compatible_with(self._shape)
    local_step = math_ops.to_int64(ops.convert_to_tensor(local_step))
    return gen_data_flow_ops.accumulator_apply_gradient(
        self._accumulator_ref, local_step=local_step, gradient=grad, name=name)

  def take_grad(self, num_required, name=None):
    """Attempts to extract the average gradient from the accumulator.

    The operation blocks until sufficient number of gradients have been
    successfully applied to the accumulator.

    Once successful, the following actions are also triggered:
    - Counter of accumulated gradients is reset to 0.
    - Aggregated gradient is reset to 0 tensor.
    - Accumulator's internal time step is incremented by 1.

    Args:
      num_required: Number of gradients that needs to have been aggregated
      name: Optional name for the operation

    Returns:
      A tensor holding the value of the average gradient.

    Raises:
      InvalidArgumentError: If num_required < 1
    """
    return gen_data_flow_ops.accumulator_take_gradient(
        self._accumulator_ref, num_required, dtype=self._dtype, name=name)


class SparseConditionalAccumulator(ConditionalAccumulatorBase):
  """A conditional accumulator for aggregating sparse gradients.

  Sparse gradients are represented by IndexedSlices.

  Up-to-date gradients (i.e., time step at which gradient was computed is
  equal to the accumulator's time step) are added to the accumulator.

  Extraction of the average gradient is blocked until the required number of
  gradients has been accumulated.

  Args:
    dtype: Datatype of the accumulated gradients.
    shape: Shape of the accumulated gradients.
    shared_name: Optional. If non-empty, this accumulator will be shared under
      the given name across multiple sessions.
    name: Optional name for the accumulator.
  """

  def __init__(self,
               dtype,
               shape=None,
               shared_name=None,
               name="sparse_conditional_accumulator"):
    accumulator_ref = gen_data_flow_ops.sparse_conditional_accumulator(
        dtype=dtype, shape=shape, shared_name=shared_name, name=name)
    super(SparseConditionalAccumulator,
          self).__init__(dtype, shape, accumulator_ref)

  def apply_indexed_slices_grad(self, grad, local_step=0, name=None):
    """Attempts to apply a gradient to the accumulator.

    The attempt is silently dropped if the gradient is stale, i.e., local_step
    is less than the accumulator's global time step.

    Args:
      grad: The gradient IndexedSlices to be applied.
      local_step: Time step at which the gradient was computed.
      name: Optional name for the operation.

    Returns:
      The operation that (conditionally) applies a gradient to the accumulator.

    Raises:
      InvalidArgumentError: If grad is of the wrong shape
    """
    return self.apply_grad(
        grad_indices=grad.indices,
        grad_values=grad.values,
        grad_shape=grad.dense_shape,
        local_step=local_step,
        name=name)

  def apply_grad(self,
                 grad_indices,
                 grad_values,
                 grad_shape=None,
                 local_step=0,
                 name=None):
    """Attempts to apply a sparse gradient to the accumulator.

    The attempt is silently dropped if the gradient is stale, i.e., local_step
    is less than the accumulator's global time step.

    A sparse gradient is represented by its indices, values and possibly empty
    or None shape. Indices must be a vector representing the locations of
    non-zero entries in the tensor. Values are the non-zero slices of the
    gradient, and must have the same first dimension as indices, i.e., the nnz
    represented by indices and values must be consistent. Shape, if not empty or
    None, must be consistent with the accumulator's shape (if also provided).

    Example:
      A tensor [[0, 0], [0. 1], [2, 3]] can be represented
        indices: [1,2]
        values: [[0,1],[2,3]]
        shape: [3, 2]

    Args:
      grad_indices: Indices of the sparse gradient to be applied.
      grad_values: Values of the sparse gradient to be applied.
      grad_shape: Shape of the sparse gradient to be applied.
      local_step: Time step at which the gradient was computed.
      name: Optional name for the operation.

    Returns:
      The operation that (conditionally) applies a gradient to the accumulator.

    Raises:
      InvalidArgumentError: If grad is of the wrong shape
    """
    local_step = math_ops.to_int64(ops.convert_to_tensor(local_step))
    return gen_data_flow_ops.sparse_accumulator_apply_gradient(
        self._accumulator_ref,
        local_step=local_step,
        gradient_indices=grad_indices,
        gradient_values=grad_values,
        gradient_shape=[] if grad_shape is None else grad_shape,
        has_known_shape=(grad_shape is not None),
        name=name)

  def take_grad(self, num_required, name=None):
    """Attempts to extract the average gradient from the accumulator.

    The operation blocks until sufficient number of gradients have been
    successfully applied to the accumulator.

    Once successful, the following actions are also triggered:
    - Counter of accumulated gradients is reset to 0.
    - Aggregated gradient is reset to 0 tensor.
    - Accumulator's internal time step is incremented by 1.

    Args:
      num_required: Number of gradients that needs to have been aggregated
      name: Optional name for the operation

    Returns:
      A tuple of indices, values, and shape representing the average gradient.

    Raises:
      InvalidArgumentError: If num_required < 1
    """
    return gen_data_flow_ops.sparse_accumulator_take_gradient(
        self._accumulator_ref, num_required, dtype=self._dtype, name=name)

  def take_indexed_slices_grad(self, num_required, name=None):
    """Attempts to extract the average gradient from the accumulator.

    The operation blocks until sufficient number of gradients have been
    successfully applied to the accumulator.

    Once successful, the following actions are also triggered:
    - Counter of accumulated gradients is reset to 0.
    - Aggregated gradient is reset to 0 tensor.
    - Accumulator's internal time step is incremented by 1.

    Args:
      num_required: Number of gradients that needs to have been aggregated
      name: Optional name for the operation

    Returns:
      An IndexedSlices holding the value of the average gradient.

    Raises:
      InvalidArgumentError: If num_required < 1
    """
    return_val = gen_data_flow_ops.sparse_accumulator_take_gradient(
        self._accumulator_ref, num_required, dtype=self._dtype, name=name)
    return ops.IndexedSlices(
        indices=return_val.indices,
        values=return_val.values,
        dense_shape=return_val.shape)


ops.RegisterShape("AccumulatorNumAccumulated")(common_shapes.scalar_shape)
ops.RegisterShape("AccumulatorSetGlobalStep")(common_shapes.no_outputs)

ops.RegisterShape("ConditionalAccumulator")(common_shapes.scalar_shape)

ops.RegisterShape("AccumulatorApplyGradient")(common_shapes.no_outputs)
ops.RegisterShape("AccumulatorTakeGradient")(common_shapes.unknown_shape)

ops.RegisterShape("SparseConditionalAccumulator")(common_shapes.scalar_shape)
ops.RegisterShape("SparseAccumulatorApplyGradient")(common_shapes.no_outputs)
ops.RegisterShape("SparseAccumulatorTakeGradient")(common_shapes.unknown_shape)
