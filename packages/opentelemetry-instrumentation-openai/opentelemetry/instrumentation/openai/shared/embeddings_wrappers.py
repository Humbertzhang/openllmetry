import logging
import time

from opentelemetry import context as context_api
from opentelemetry.metrics import Counter, Histogram
from opentelemetry.semconv.ai import SpanAttributes, LLMRequestTypeValues

from opentelemetry.instrumentation.utils import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.instrumentation.openai.utils import (
    _with_tracer_wrapper,
    start_as_current_span_async,
    _with_embeddings_metric_wrapper,
)
from opentelemetry.instrumentation.openai.shared import (
    _set_request_attributes,
    _set_span_attribute,
    _set_response_attributes,
    should_send_prompts,
    model_as_dict, _get_openai_base_url, OPENAI_LLM_USAGE_TOKEN_TYPES,
)

from opentelemetry.instrumentation.openai.utils import is_openai_v1

from opentelemetry.trace import SpanKind

SPAN_NAME = "openai.embeddings"
LLM_REQUEST_TYPE = LLMRequestTypeValues.EMBEDDING

logger = logging.getLogger(__name__)


@_with_embeddings_metric_wrapper
def embeddings_metrics_wrapper(token_counter: Counter,
                               vector_size_counter: Counter,
                               duration_histogram: Histogram,
                               wrapped, instance, args, kwargs):

    if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
        return wrapped(*args, **kwargs)

    try:
        # record time for duration
        start_time = time.time()
        response = wrapped(*args, **kwargs)
        end_time = time.time()
    except Exception as e:  # pylint: disable=broad-except
        end_time = time.time()
        duration = end_time - start_time if 'start_time' in locals() else 0
        attributes = {
            "error.type": e.__class__.__name__,
            "server.address": _get_openai_base_url(),
        }

        token_counter.add(1, attributes=attributes)
        vector_size_counter.add(1, attributes=attributes)
        # if there are legal duration, record it
        if duration > 0:
            duration_histogram.record(duration, attributes=attributes)

        raise e

    if is_openai_v1():
        response_dict = model_as_dict(response)
    else:
        response_dict = response

    shared_attributes = {
        "llm.response.model": response_dict.get("model") or None,
        "server.address": _get_openai_base_url(),
    }

    # token
    usage = response_dict.get("usage")
    if usage is not None:
        for name, val in usage.items():
            if name in OPENAI_LLM_USAGE_TOKEN_TYPES:
                attributes_with_token_type = {**shared_attributes, "llm.usage.token_type": name.split('_')[0]}
                token_counter.add(val, attributes=attributes_with_token_type)

    # vec size
    # should use counter for vector_size?
    vec_embedding = (response_dict.get("data") or [{}])[0].get("embedding", [])
    vec_size = len(vec_embedding)
    vector_size_counter.add(vec_size, attributes=shared_attributes)

    # duration
    duration = end_time - start_time
    duration_histogram.record(duration, attributes=shared_attributes)

    return response


@_with_tracer_wrapper
def embeddings_wrapper(tracer, wrapped, instance, args, kwargs):
    if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
        return wrapped(*args, **kwargs)

    with tracer.start_as_current_span(
        name=SPAN_NAME,
        kind=SpanKind.CLIENT,
        attributes={SpanAttributes.LLM_REQUEST_TYPE: LLM_REQUEST_TYPE.value},
    ) as span:
        _handle_request(span, kwargs)
        response = wrapped(*args, **kwargs)
        _handle_response(response, span)

        return response


@_with_tracer_wrapper
async def aembeddings_wrapper(tracer, wrapped, instance, args, kwargs):
    if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
        return wrapped(*args, **kwargs)

    async with start_as_current_span_async(
        tracer=tracer,
        name=SPAN_NAME,
        kind=SpanKind.CLIENT,
        attributes={SpanAttributes.LLM_REQUEST_TYPE: LLM_REQUEST_TYPE.value},
    ) as span:
        _handle_request(span, kwargs)
        response = await wrapped(*args, **kwargs)
        _handle_response(response, span)

        return response


def _handle_request(span, kwargs):
    _set_request_attributes(span, kwargs)
    if should_send_prompts():
        _set_prompts(span, kwargs.get("input"))


def _handle_response(response, span):
    if is_openai_v1():
        response_dict = model_as_dict(response)
    else:
        response_dict = response

    _set_response_attributes(span, response_dict)


def _set_prompts(span, prompt):
    if not span.is_recording() or not prompt:
        return

    try:
        if isinstance(prompt, list):
            for i, p in enumerate(prompt):
                _set_span_attribute(
                    span, f"{SpanAttributes.LLM_PROMPTS}.{i}.content", p
                )
        else:
            _set_span_attribute(
                span,
                f"{SpanAttributes.LLM_PROMPTS}.0.content",
                prompt,
            )
    except Exception as ex:  # pylint: disable=broad-except
        logger.warning("Failed to set prompts for openai span, error: %s", str(ex))
