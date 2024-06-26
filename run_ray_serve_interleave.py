"""TODO(ricliu): DO NOT SUBMIT without one-line documentation for run_server.

TODO(ricliu): DO NOT SUBMIT without a detailed description of run_server.
"""

import os
import random
import time
from typing import Sequence
from absl import app, flags
from io import BytesIO  # pylint:disable=g-importing-member
from typing import List
from typing import Any, AsyncIterator

from ray import serve
from ray.serve.config import gRPCOptions

from starlette.requests import Request
from starlette.responses import Response
import json

import jax
from jetstream.core import server_lib
from jetstream.core import config_lib
from jetstream.core import orchestrator
from jetstream.core.config_lib import ServerConfig
from jetstream_pt import ray_engine
from jetstream_pt.config import FLAGS

import numpy as np
from jetstream.engine import token_utils
#from jetstream_pt import engine as je

import grpc
from jetstream.core.proto import jetstream_pb2
from jetstream.core.proto import jetstream_pb2_grpc
#from jetstream.core.proto.jetstream_pb2_grpc import add_OrchestratorServicer_to_server


flags.DEFINE_integer("port", 9000, "port to listen on")
flags.DEFINE_integer("threads", 64, "number of worker threads in thread pool")
flags.DEFINE_string(
    "config",
    "InterleavedCPUTestServer",
    "available servers",
)
flags.DEFINE_integer("prometheus_port", 0, "")
flags.DEFINE_integer("tpu_chips", 16, "device tpu_chips")

flags.DEFINE_bool("enable_jax_profiler", False, "enable jax profiler")
flags.DEFINE_integer("jax_profiler_port", 9999, "port of JAX profiler server")

flags.DEFINE_bool(
    "is_disaggregated", False, "Disaggregated serving if it's True"
)

flags.DEFINE_integer("num_hosts", 4, "Number of TPU host", required=False)

flags.DEFINE_string("decode_pod_slice_name", "", "Decode pod slice name")


def create_engine(**kwargs):
  """create a pytorch engine"""
  jax.config.update("jax_default_prng_impl", "unsafe_rbg")
  os.environ["TF_CPP_MIN_LOG_LEVEL"] = "0"

  start = time.perf_counter()
  engine = ray_engine.create_pytorch_ray_engine(
      model_name=kwargs['model_name'],
      tokenizer_path=kwargs['tokenizer_path'],
      ckpt_path=kwargs['ckpt_path'],
      bf16_enable=kwargs['bf16_enable'],
      param_size=kwargs['param_size'],
      context_length=kwargs['context_length'],
      batch_size=kwargs['batch_size'],
      quantize_weights=kwargs['quantize_weights'],
      quantize_kv=kwargs['quantize_kv'],
      max_cache_length=kwargs['max_cache_length'],
      sharding_config=kwargs['sharding_config'],
      enable_jax_profiler=kwargs['enable_jax_profiler'],
      jax_profiler_port=kwargs['jax_profiler_port'],
  )

  print("Initialize engine", time.perf_counter() - start)
  return engine


def create_disaggregated_engine():
  """create a pytorch engine"""
  jax.config.update("jax_default_prng_impl", "unsafe_rbg")
  os.environ["TF_CPP_MIN_LOG_LEVEL"] = "0"

  start = time.perf_counter()
  prefill_engine_list, decode_engine_list = (
      ray_engine.create_pytorch_ray_engine(
          model_name=FLAGS.model_name,
          tokenizer_path=FLAGS.tokenizer_path,
          ckpt_path=FLAGS.checkpoint_path,
          bf16_enable=FLAGS.bf16_enable,
          param_size=FLAGS.size,
          context_length=FLAGS.context_length,
          batch_size=FLAGS.batch_size,
          quantize_weights=FLAGS.quantize_weights,
          quantize_kv=FLAGS.quantize_kv_cache,
          max_cache_length=FLAGS.max_cache_length,
          sharding_config=FLAGS.sharding_config,
          enable_jax_profiler=FLAGS.enable_jax_profiler,
          jax_profiler_port=FLAGS.jax_profiler_port,
          is_disaggregated=FLAGS.is_disaggregated,
          num_hosts=FLAGS.num_hosts,
          decode_pod_slice_name=FLAGS.decode_pod_slice_name,
      )
  )

  print("Initialize engine", time.perf_counter() - start)
  return (prefill_engine_list, decode_engine_list)

@serve.deployment(
   ray_actor_options={
      "resources": {"TPU-v4-8-head": 1},
   })
class JetStreamDeployment:
  def __init__(self, **kwargs):
    os.environ["XLA_FLAGS"] = "--xla_dump_to=/tmp/xla_logs --xla_dump_hlo_as_text"
    devices = []
    for i in range(kwargs['tpu_chips']):
      devices.append(i)

    print(f"devices: {devices}")

    self.batch_size = kwargs['batch_size']
    self.threads = kwargs['threads']
    self.port = kwargs['port']
    self.devices = devices

    #if FLAGS.is_disaggregated:
    #  prefill_engine_list, decode_engine_list = create_disaggregated_engine()
    #  chips = int(len(devices) / 2)
    #  server_config = ServerConfig(
    #    prefill_slices=(f"tpu={chips}",),
    #    prefill_engine_create_fns=(lambda a: prefill_engine_list[0],),
    #    generate_slices=(f"tpu={chips}",),
    #    generate_engine_create_fns=(lambda a: decode_engine_list[0],),
    #    is_ray_backend=True,
    #)

    #else:


    self.engine = create_engine(**kwargs)
    server_config = ServerConfig(
      interleaved_slices=(f"tpu={len(devices)}",),
      interleaved_engine_create_fns=(lambda a: self.engine,),
    )


    engines = config_lib.get_engines(server_config, devices=devices)
    prefill_params = [pe.load_params() for pe in engines.prefill_engines]
    generate_params = [ge.load_params() for ge in engines.generate_engines]
    shared_params = [ie.load_params() for ie in engines.interleaved_engines]
    print("Loaded all weights.")


    self.driver = orchestrator.Driver(
      prefill_engines=engines.prefill_engines + engines.interleaved_engines,
      generate_engines=engines.generate_engines + engines.interleaved_engines,
      prefill_params=prefill_params + shared_params,
      generate_params=generate_params + shared_params,
      interleaved_mode=True,
      jax_padding=False,
      metrics_collector=None,
      is_ray_backend=True,
    )

    self.orchestrator = orchestrator.LLMOrchestrator(driver=self.driver)

    print("Started jetstream driver....")


  async def Decode(self, request: jetstream_pb2.DecodeRequest) -> AsyncIterator[jetstream_pb2.DecodeResponse]:

    return self.orchestrator.Decode(request)


def main(_argv):
  deployment = JetStreamDeployment.bind(
    tpu_chips=FLAGS.tpu_chips,
    threads=FLAGS.threads,
    port=FLAGS.port,
    model_name=FLAGS.model_name,
    tokenizer_path=FLAGS.tokenizer_path,
    ckpt_path=FLAGS.checkpoint_path,
    bf16_enable=FLAGS.bf16_enable,
    param_size=FLAGS.size,
    context_length=FLAGS.context_length,
    batch_size=FLAGS.batch_size,
    quantize_weights=FLAGS.quantize_weights,
    quantize_kv=FLAGS.quantize_kv_cache,
    max_cache_length=FLAGS.max_cache_length,
    sharding_config=FLAGS.sharding_config,
    enable_jax_profiler=FLAGS.enable_jax_profiler,
    jax_profiler_port=FLAGS.jax_profiler_port,
  )

  grpc_port = 8888
  grpc_servicer_functions = [
    "jetstream.core.proto.jetstream_pb2_grpc.add_OrchestratorServicer_to_server",
  ]
  serve.start(
    grpc_options=gRPCOptions(
      port=grpc_port,
      grpc_servicer_functions=grpc_servicer_functions,
    ),
  )

  serve.run(deployment)

if __name__ == "__main__":
  app.run(main)
