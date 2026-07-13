import argparse
import os
import unittest
from unittest.mock import patch

from verilog_agent.llm_config import (
    add_llm_arguments,
    llm_config,
    normalize_chat_completions_url,
    resolve_llm_settings,
)


class LlmConfigurationTests(unittest.TestCase):
    def test_gpt_oss_remote_endpoint_uses_openai_compatible_backend(self):
        with patch.dict(
            os.environ,
            {
                "LLM_PROVIDER": "gpt-oss",
                "GPT_OSS_MODEL": "custom-rtl-model",
                "GPT_OSS_API_URL": "http://abc.net:30001/chat/completions",
                "GPT_OSS_API_KEY": "secret-key",
                "LLM_TIMEOUT_SECONDS": "75",
            },
            clear=True,
        ):
            settings = resolve_llm_settings()

        self.assertEqual(settings["provider"], "gpt-oss")
        self.assertEqual(settings["backend"], "openai-compatible")
        self.assertEqual(settings["model"], "custom-rtl-model")
        self.assertEqual(settings["base_url"], "http://abc.net:30001")
        self.assertEqual(settings["timeout_seconds"], 75)

    def test_cli_values_override_environment(self):
        with patch.dict(
            os.environ,
            {
                "LLM_PROVIDER": "ollama",
                "LLM_MODEL": "environment-model",
                "LLM_TEMPERATURE": "0.9",
            },
            clear=True,
        ):
            settings = resolve_llm_settings(
                provider="openai",
                model="cli-model",
                temperature=0.2,
                api_key="cli-secret",
                timeout_seconds=10,
            )

        self.assertEqual(settings["provider"], "openai")
        self.assertEqual(settings["model"], "cli-model")
        self.assertEqual(settings["temperature"], 0.2)
        self.assertEqual(settings["api_key"], "cli-secret")
        self.assertEqual(settings["timeout_seconds"], 10)

    def test_public_config_redacts_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            config = llm_config(
                provider="gpt-oss",
                model="rtl-model",
                api_url="http://abc.net:30001/chat/completions",
                api_key="1234567890abcdef",
            )

        self.assertTrue(config["api_key_set"])
        self.assertEqual(config["api_key_redacted"], "1234...cdef")
        self.assertNotIn("1234567890abcdef", config.values())

    def test_llm_arguments_are_registered_by_separate_module(self):
        parser = argparse.ArgumentParser()
        add_llm_arguments(parser)
        args = parser.parse_args(
            [
                "--llm-provider",
                "gpt-oss",
                "--llm-model",
                "rtl-model",
                "--llm-temperature",
                "0.3",
                "--llm-timeout",
                "45",
            ]
        )

        self.assertEqual(args.llm_provider, "gpt-oss")
        self.assertEqual(args.llm_model, "rtl-model")
        self.assertEqual(args.llm_temperature, 0.3)
        self.assertEqual(args.llm_timeout, 45)

    def test_chat_completions_suffix_is_removed_once(self):
        self.assertEqual(
            normalize_chat_completions_url("http://abc.net:30001/chat/completions/"),
            "http://abc.net:30001",
        )


if __name__ == "__main__":
    unittest.main()
