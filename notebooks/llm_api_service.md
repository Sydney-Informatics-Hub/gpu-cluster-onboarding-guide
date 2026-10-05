# Connecting to the Self-Hosted LLM API Service

The Apollo GPU cluster provides a way to use large language models (e.g. Qwen3.8) that are hosted entirely in-house, instead of sending your prompts to an external vendor. Your data stays within the university's own infrastructure.

:::{.callout-important}
Formal data protection and security review for this service are still a work in progress. Treat it accordingly until that review is complete - check with SIH before sending sensitive or restricted data.
:::

This page covers how to request access and configure common tools:
* VS Code
* Claude Code
* [pi](https://pi.dev)

## Requesting access

Access is currently gated as part of a small pilot. To request a personal API key:

1. Go to [sih.tools/request](https://sih.tools/request).
2. Select **Short support for research computing facilities**.
3. Select **Apollo GPU cluster**.
4. Under **What assistance do you require?**, select **Personal API key for LLM access**.
5. Complete the remaining details and submit.

You'll receive your endpoint URL, API key, and available model name(s) by reply.

## What you'll need

In your choice of IDE, you will need to configure the following:

- **Base URL** - `https://<PROJECT_ID>-litellm-proxy.gpu.sydney.edu.au` (yours will be given to you when your key is issued)
- **API key** - an `sk-...` string issued to you

## Verifying your key works

Before configuring any tool, confirm your key and endpoint are actually valid with a quick `curl` check:

```bash
curl -X GET https://<PROJECT_ID>-litellm-proxy.gpu.sydney.edu.au/v1/agents \
  -H "Authorization: Bearer $API" \
  -H "Content-Type: application/json"
```

A working key returns a JSON response (e.g. an empty list) rather than an error. A `401` means the key or `Authorization` header is wrong; a connection timeout/failure usually means the base URL or `<PROJECT_ID>` is wrong, or you're not on a network that can reach it.

::::{.panel-tabset}

## How to use in VS Code

VS Code's Copilot Chat supports "Bring Your Own Key" against any OpenAI-compatible endpoint.

**Option A — via the command palette:**

1. Open the Command Palette and run `Chat: Manage Language Models`.
2. Select the **OpenAI Compatible** provider.
3. Enter your base URL (with `/v1` appended, e.g. `https://<PROJECT_ID>-litellm-proxy.gpu.sydney.edu.au/v1`), your API key, and the model ID given to you.
4. The model now appears in the Chat model dropdown.

**Option B — via `settings.json`:**

```json
"github.copilot.chat.customOAIModels": {
  "<model-id>": {
    "name": "Apollo GPU Cluster - <model-id>",
    "url": "https://<PROJECT_ID>-litellm-proxy.gpu.sydney.edu.au/v1/chat/completions",
    "toolCalling": true,
    "maxInputTokens": 32000,
    "maxOutputTokens": 8000
  }
}
```

:::{.callout-important}
Set `"toolCalling": true`. Without it, Copilot silently falls back to plain chat and agent/tool features won't work.
:::

## How to use in Claude Code

[Claude Code](https://claude.com/claude-code) speaks Anthropic's API format, not OpenAI's. LiteLLM translates Anthropic-format requests to whatever model is actually being served, so this works even when the served model isn't a Claude model (e.g. Qwen).

Configure it with environment variables:

```bash
export ANTHROPIC_BASE_URL="https://<PROJECT_ID>-litellm-proxy.gpu.sydney.edu.au"
export ANTHROPIC_AUTH_TOKEN="<your-api-key>"
export ANTHROPIC_MODEL="<model-id>"
```

Note that `ANTHROPIC_BASE_URL` does **not** take a `/v1` suffix here - unlike the OpenAI-compatible tools above, LiteLLM handles Anthropic-format routing itself.

Optionally, set `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` before launching `claude` to have the `/model` picker list models discovered directly from the proxy.

:::{.callout-important}
As with pi, tool/agent features only work if the backend model was served with tool calling enabled (vLLM's `--enable-auto-tool-choice` flag plus a matching `--tool-call-parser`). This is a server-side setting - check with whoever issued your key if agent/tool features silently don't work.
:::

## How to use in pi

[pi](https://pi.dev) is a CLI coding agent that explicitly supports vLLM and other OpenAI-compatible backends. Add a provider entry to `~/.pi/agent/models.json`:

```json
{
  "providers": {
    "apollo": {
      "baseUrl": "https://<PROJECT_ID>-litellm-proxy.gpu.sydney.edu.au/v1",
      "api": "openai-completions",
      "apiKey": "<your-api-key>",
      "models": [{ "id": "<model-id>" }]
    }
  }
}
```

This file is reloaded each time you open `/model` in pi, so no restart is needed after editing it.

:::{.callout-important}
Tool calling only works if the model was served with automatic tool selection enabled (vLLM's `--enable-auto-tool-choice` flag plus a matching `--tool-call-parser`, e.g. `hermes` for Qwen3). This is a server-side setting, not something you can turn on from pi — if agent/tool features silently don't work, check with whoever issued your key that the backend was started with these flags.
:::

::::

:::{.callout-note}
The model name used in the examples above (`<model-id>`) is a placeholder. Use the exact model name given to you when your API key was issued - the model lineup may change during the pilot.
:::
