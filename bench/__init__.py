"""The benchmark runs on vLLM, where the chat template takes the thinking switch.
deltos itself sends no server-specific fields (deltos/llm.py); every bench entry
point (python3 -m bench.*) sets them here, so its requests stay what they were."""
from deltos.llm import configure as _configure

_configure(thinking_off={"chat_template_kwargs": {"enable_thinking": False}},
           thinking_on={"chat_template_kwargs": {"enable_thinking": True}})
