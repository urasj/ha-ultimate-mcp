"""assist/ surface manifest — conversation-agent + Assist-pipeline test harness (W5).

Pure data. Read-only tools that run/inspect the conversation and assist_pipeline
subsystems (this box runs Google AI conversation agents). Gated on
integration:conversation. No mutating tools: running an agent/pipeline has no
persistent side effect beyond whatever intents the utterance triggers, so these
stay T0 — but note that a conversation utterance CAN call services, so operators
should pass benign test utterances.
"""

from ultimate_mcp.spec import SurfaceSpec, Tier, ToolSpec

SURFACE = SurfaceSpec(
    name="assist",
    summary="Assist test harness: list conversation agents, run an utterance through an agent "
    "or a full pipeline, diff two agents on the same utterance, and lint Assist entity exposure",
    impl_module="ultimate_mcp.tools.assist.impl",
    requires=("integration:conversation",),
    tools=(
        ToolSpec(
            name="conversation_agents",
            summary="List conversation agents available on this instance",
            tier=Tier.T0_READ,
            keywords=("conversation", "agents", "assist", "llm", "google", "list"),
        ),
        ToolSpec(
            name="conversation_test",
            summary="Send an utterance to a conversation agent and return the response + intent "
            "(conversation/process)",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The utterance to process"},
                    "agent_id": {
                        "type": ["string", "null"],
                        "default": None,
                        "description": "Agent to target (null = default agent)",
                    },
                    "language": {"type": ["string", "null"], "default": None},
                    "conversation_id": {"type": ["string", "null"], "default": None},
                },
                "required": ["text"],
            },
            keywords=("conversation", "test", "process", "utterance", "intent", "agent", "ask"),
        ),
        ToolSpec(
            name="pipeline_list",
            summary="List Assist pipelines (assist_pipeline/pipeline/list)",
            tier=Tier.T0_READ,
            keywords=("pipeline", "assist", "list", "stt", "tts", "voice"),
        ),
        ToolSpec(
            name="pipeline_run",
            summary="Run text through an Assist pipeline, timeboxed (assist_pipeline/run)",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "pipeline_id": {"type": ["string", "null"], "default": None},
                    "timeout": {"type": "number", "default": 30, "minimum": 0.1, "maximum": 120},
                },
                "required": ["text"],
            },
            keywords=("pipeline", "run", "assist", "voice", "harness", "intent", "timebox"),
        ),
        ToolSpec(
            name="agent_diff",
            summary="Run the same utterance through two agents and diff their responses",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "agent_id_a": {"type": "string"},
                    "agent_id_b": {"type": "string"},
                    "language": {"type": ["string", "null"], "default": None},
                },
                "required": ["text", "agent_id_a", "agent_id_b"],
            },
            keywords=("diff", "compare", "agents", "conversation", "ab", "regression"),
        ),
        ToolSpec(
            name="assist_exposure_lint",
            summary="Report entities exposed to Assist vs. not, from the entity registry "
            "conversation exposure options",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": ["string", "null"],
                        "default": None,
                        "description": "Limit to one domain, e.g. 'light'",
                    }
                },
            },
            keywords=("exposure", "exposed", "assist", "lint", "entities", "voice", "audit"),
        ),
    ),
)
