# Copyright 2021 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os

import jax
import jax.numpy as jnp
import numpy as np
from absl.testing import absltest
from jax._src import xla_bridge
from jax._src import test_util as jtu
from jax.sharding import NamedSharding, PartitionSpec as P
from jax.experimental.shard_alike import shard_alike
from jax.experimental.shard_map import shard_map
from jax._src.lib import xla_extension_version

from jax import config
config.parse_flags_with_absl()

prev_xla_flags = None


def setUpModule():
  global prev_xla_flags
  prev_xla_flags = os.getenv("XLA_FLAGS")
  flags_str = prev_xla_flags or ""
  # Don't override user-specified device count, or other XLA flags.
  if "xla_force_host_platform_device_count" not in flags_str:
    os.environ["XLA_FLAGS"] = (flags_str +
                               " --xla_force_host_platform_device_count=8")
  # Clear any cached backends so new CPU backend will pick up the env var.
  xla_bridge.get_backend.cache_clear()

def tearDownModule():
  if prev_xla_flags is None:
    del os.environ["XLA_FLAGS"]
  else:
    os.environ["XLA_FLAGS"] = prev_xla_flags
  xla_bridge.get_backend.cache_clear()


class ShardAlikeTest(jtu.JaxTestCase):

  def setUp(self):
    super().setUp()
    if xla_extension_version < 227:
      self.skipTest('Requires xla_extension_version >= 227')

  def test_basic(self):
    mesh = jtu.create_global_mesh((2, 2), ('x', 'y'))
    np_inp = np.arange(16).reshape(8, 2)
    s = NamedSharding(mesh, P('x', 'y'))
    inp = jax.device_put(np_inp, s)

    @jax.jit
    def f(x):
      y = x * x
      z = y * 2
      _, z = shard_alike(x, z)
      return z * 2

    out = f(inp)
    self.assertEqual(out.sharding, s)
    self.assertArraysEqual(out, np_inp * np_inp * 4)

  def test_output_sharded_alike_input(self):
    mesh = jtu.create_global_mesh((2, 1), ('x', 'y'))
    np_inp = np.arange(16).reshape(8, 2)
    s = NamedSharding(mesh, P('x', 'y'))
    inp = jax.device_put(np_inp, s)

    @jax.jit
    def f(x):
      y = x * 2
      return shard_alike(x, y)[1]

    out = f(inp)
    self.assertEqual(out.sharding, s)
    self.assertArraysEqual(out, np_inp * 2)

  def test_arange_shard_alike_jit(self):
    mesh = jtu.create_global_mesh((2, 1), ('x', 'y'))
    np_inp = np.arange(16).reshape(8, 2)
    s = NamedSharding(mesh, P('x', 'y'))
    inp = jax.device_put(np_inp, s)

    @jax.jit
    def f(x):
      y = jnp.arange(16).reshape(8, 2)
      return shard_alike(x, y)[1]

    out = f(inp)
    self.assertEqual(out.sharding, s)
    self.assertArraysEqual(out, np_inp)

  def test_different_shapes(self):
    mesh = jtu.create_global_mesh((2, 1), ('x', 'y'))
    np_inp = np.arange(16).reshape(8, 2)
    s = NamedSharding(mesh, P('x',))
    inp = jax.device_put(np_inp, s)

    @jax.jit
    def f(x):
      y = x @ x.T
      return shard_alike(x, y)[1]

    with self.assertRaisesRegex(
        ValueError, 'The leaves shapes of `x` and `y` should match'):
      f(inp)

  def test_double_shard_alike(self):
    mesh = jtu.create_global_mesh((2, 2), ('x', 'y'))
    np_inp = np.arange(16).reshape(8, 2)
    s = NamedSharding(mesh, P('x', 'y'))
    inp = jax.device_put(np_inp, s)

    @jax.jit
    def f(x):
      y = x * 2
      _, y = shard_alike(x, y)
      z = y @ y.T
      a = jnp.arange(64).reshape(8, 8)
      return shard_alike(z, a)

    out1, out2 = f(inp)
    self.assertEqual(out1.sharding, NamedSharding(mesh, P('x')))
    self.assertEqual(out2.sharding, NamedSharding(mesh, P('x')))

  def test_shard_like_eager(self):
    mesh = jtu.create_global_mesh((4, 1), ('x', 'y'))
    np_inp = np.arange(16).reshape(8, 2)
    s = NamedSharding(mesh, P('x', 'y'))
    inp = jax.device_put(np_inp, s)

    def f(x):
      y = jnp.arange(16).reshape(8, 2)
      return shard_alike(x, y)[1]

    out = f(inp)
    self.assertEqual(out.sharding, s)
    self.assertArraysEqual(out, np_inp)

  def test_shard_map(self):
    mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    np_inp = np.arange(16).reshape(8, 2)
    s = NamedSharding(mesh, P('x', 'y'))
    inp = jax.device_put(np_inp, s)

    def g(x):
      return jax.lax.psum(x, 'x')

    @jax.jit
    def f(x):
      y = x @ x.T
      s_out = shard_map(g, mesh, in_specs=P('x', 'y'),
                        out_specs=P(None, 'y'))(y)
      z = s_out.T @ s_out
      return shard_alike(y, z)

    out1, out2 = f(inp)
    # From options; P('x', 'y'), P('y'), shard_like chooses the better option.
    self.assertEqual(out1.sharding, s)
    self.assertEqual(out2.sharding, s)

  def test_grad(self):
    mesh = jtu.create_global_mesh((4,), ('x',))
    np_inp = np.arange(8.)
    s = NamedSharding(mesh, P('x'))
    inp = jax.device_put(np_inp, s)

    def _cb(s):
      self.assertFalse(s.is_fully_replicated)
      self.assertLen(s.device_set, mesh.size)
      self.assertEqual(s.shard_shape(np_inp.shape), (2,))

    def f(x):
      y = jnp.arange(8.)
      x_, y_ = shard_alike(x, y)
      jax.debug.inspect_array_sharding(y_, callback=_cb)
      z = x_ + y_
      return jnp.sum(z)

    jax.grad(f)(inp)  # doesn't crash
    jax.grad(jax.jit(f))(inp)  # doesn't crash

  def test_shard_input_as_output(self):
    mesh = jtu.create_global_mesh((4,), ('x',))
    np_inp = np.arange(8.)
    s = NamedSharding(mesh, P('x'))

    @jax.jit
    def f(x):
      y = jax.lax.with_sharding_constraint(x, s)
      z = y * 2
      return shard_alike(x, z)

    with jtu.count_pjit_cpp_cache_miss() as count:
      f(np_inp)
      out1, out2 = f(np_inp)
    self.assertEqual(count[0], 1)
    self.assertTrue(s.is_equivalent_to(out1.sharding, np_inp.ndim))
    self.assertTrue(s.is_equivalent_to(out2.sharding, np_inp.ndim))

    @jax.jit
    def g(x):
      z = x * 2
      return shard_alike(x, z)
    arr = jax.device_put(np_inp, s)
    with jtu.count_pjit_cpp_cache_miss() as count:
      g(arr)
      out3, out4 = g(arr)
    self.assertEqual(count[0], 1)
    self.assertEqual(out3.sharding, s)
    self.assertEqual(out4.sharding, s)

  def test_shard_alike_inputs(self):
    mesh = jtu.create_global_mesh((2,), ('x',))
    np_inp = np.arange(8.)
    s = NamedSharding(mesh, P('x'))
    rep_s = NamedSharding(mesh, P())
    arr = jax.device_put(np_inp, s)
    arr2 = jax.device_put(np_inp, rep_s)

    def f(x, y):
      return shard_alike(x, y)

    eager_out1, eager_out2 = f(arr, arr2)
    self.assertEqual(eager_out1.sharding, s)
    self.assertEqual(eager_out2.sharding, s)

    out1, out2 = jax.jit(f)(arr, arr2)
    self.assertEqual(out1.sharding, s)
    self.assertEqual(out2.sharding, s)


if __name__ == '__main__':
  absltest.main(testLoader=jtu.JaxTestLoader())
