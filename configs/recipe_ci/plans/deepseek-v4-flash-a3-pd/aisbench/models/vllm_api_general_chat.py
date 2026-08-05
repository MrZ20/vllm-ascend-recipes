import os

from ais_bench.benchmark.models import VLLMCustomAPIChat
from ais_bench.benchmark.utils.postprocess.model_postprocessors import (
    extract_non_reasoning_content,
)


# Accuracy request configuration generated from the Recipe. The Runner injects
# the gateway address and actual model path at runtime.
models = [
    dict(
        attr="service",
        type=VLLMCustomAPIChat,
        abbr="vllm-api-general-chat",
        path=os.environ["RECIPE_MODEL_PATH"],
        model=os.environ["RECIPE_SERVED_MODEL_NAME"],
        stream=False,
        request_rate=0,
        use_timestamp=False,
        retry=2,
        api_key="",
        host_ip=os.environ["RECIPE_ENDPOINT_HOST"],
        host_port=int(os.environ["RECIPE_ENDPOINT_PORT"]),
        url="",
        max_out_len=int(os.environ.get("RECIPE_AISBENCH_MAX_OUT_LEN", "512")),
        batch_size=1,
        trust_remote_code=False,
        generation_kwargs=dict(temperature=0.01, ignore_eos=False),
        pred_postprocessor=dict(type=extract_non_reasoning_content),
    )
]
