"""Tool Runtime 的安全边界、类型化结果与请求快照回归。"""

from __future__ import annotations
import asyncio
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.agent.react_agent import OpenAIAgent
from backend.agent.contracts import AgentRunRequest
from backend.agent.hooks import AgentPipelineDefinition, AgentRunContext
from backend.agent.hooks.decorators import wrap_tool_call
from backend.agent.hooks.types import (
    ToolStartedEvent,
    ToolCompletedEvent,
    OperationFailedEvent,
)
from backend.config.config import Settings
from backend.mcp_tool import McpToolDescriptor
from backend.permissions import PermissionDecision
from backend.tools.contracts import (
    ToolSpec,
    ToolKey,
    ToolBinding,
    ToolOutcome,
    ToolOutcomeStatus,
    ProviderToolCall,
    ToolExecutionPolicy,
    ActivateToolAllowlist,
    RecordInvokedSkill,
    UpdatePlan,
    RecordLoadedMemoryPaths,
    ToolCallRequest,
    freeze,
)
from backend.tools.toolset import RequestToolSet
from backend.tools.catalog import SkillCatalog
from backend.tools.dispatcher import ToolDispatcher
from backend.tools.adapters.skill import SkillAliasNormalizer
from backend.tools.registry import build_request_tool_set
from backend.tools.adapters.mcp import bind_mcp_tool
from backend.tools.adapters.skill import build_skill_tool
from backend.tools.validation import validate_tool_arguments


def binding(name="Read", executor=None, schema=None):
    async def execute(ctx, args):
        return ToolOutcome.succeeded("ok")

    return ToolBinding(
        ToolSpec(ToolKey("local", name), name, "test", schema or {"type": "object"}),
        executor or execute,
        lambda args: (str(args.get("path", name)),),
    )


def runtime(tool_set, *, wrappers=(), observers=(), catalog=None):
    context = AgentRunContext(metadata={"permission_mode": "default"})
    dispatcher = ToolDispatcher(tool_set, normalizer=SkillAliasNormalizer(catalog) if catalog is not None else None)
    state = {
        "config": Settings(),
        "pipeline": AgentPipelineDefinition.compile(wrappers).bind_runtime(
            context=context, observers=list(observers)
        ),
        "approved_targets": set(),
        "approval_handler": None,
        "loaded_memory_paths": set(),
        "dispatcher": dispatcher,
    }
    return dispatcher, state


async def dispatch(dispatcher, state, name="Read", arguments="{}", call_id="call"):
    return await dispatcher.dispatch(
        state, ProviderToolCall(call_id, 0, name, arguments)
    )


class ToolSetTests(unittest.TestCase):
    def test_default_presets_preserve_provider_protocol(self):
        baseline = json.loads(
            Path(__file__)
            .with_name("fixtures")
            .joinpath("tool_presets.json")
            .read_text()
        )
        for preset, expected in baseline.items():
            with self.subTest(preset=preset):
                tool_set = build_request_tool_set(
                    settings=Settings(local_tool_preset=preset),
                    mcp_tools=[],
                    mcp_manager=AsyncMock(),
                    skill_catalog=SkillCatalog(),
                    authorized_servers=set(),
                )
                actual = tool_set.provider_specs()
                for before, after in zip(expected, actual, strict=True):
                    if before["function"]["name"] == "Bash":
                        from backend.prompts.tool_guidance.shell import BASH_GUIDANCE

                        self.assertEqual(
                            before["function"]["description"], BASH_GUIDANCE
                        )
                        before["function"]["description"] = after["function"][
                            "description"
                        ]
                self.assertEqual(actual, expected)
                self.assertEqual(
                    tool_set.capability_view().names,
                    frozenset(s["function"]["name"] for s in expected),
                )
                self.assertEqual(
                    set(tool_set.context_policies()), set(tool_set.by_provider_name)
                )

    def test_schema_and_provider_projections_cannot_mutate_snapshot(self):
        schema = {"type": "object", "properties": {"x": {"enum": ["a"]}}}
        tool_set = RequestToolSet.create([binding(schema=schema)])
        schema["properties"]["x"]["enum"].append("b")
        projected = tool_set.provider_specs()
        projected[0]["function"]["parameters"]["properties"]["x"]["enum"].append("c")
        self.assertEqual(
            tool_set.provider_specs()[0]["function"]["parameters"]["properties"]["x"][
                "enum"
            ],
            ["a"],
        )
        with self.assertRaises(TypeError):
            tool_set.resolve("Read").spec.input_schema["properties"]["x"]["type"] = (
                "string"
            )
        with self.assertRaises(TypeError):
            tool_set.by_provider_name["Write"] = binding("Write")

    def test_duplicate_keys_names_invalid_names_and_schemas_fail_fast(self):
        for bindings in (
            [binding(), binding()],
            [
                binding(),
                replace(binding(), spec=replace(binding().spec, provider_name="Other")),
            ],
            [binding("bad name")],
            [binding(schema={"type": "array"})],
            [binding(schema={"type": "object", "required": "not-an-array"})],
        ):
            with self.subTest(bindings=bindings), self.assertRaises(Exception):
                RequestToolSet.create(bindings)

    def test_unknown_config_preset_and_unauthorized_mcp_fail(self):
        cases = [
            dict(settings=Settings(local_tool_names="Missing"), mcp_tools=[]),
            dict(settings=Settings(local_tool_names="Read,Read"), mcp_tools=[]),
            dict(settings=Settings(local_tool_preset="missing"), mcp_tools=[]),
            dict(
                settings=Settings(),
                mcp_tools=[McpToolDescriptor("other", "run", "", {})],
            ),
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(Exception):
                build_request_tool_set(
                    **case,
                    mcp_manager=AsyncMock(),
                    skill_catalog=SkillCatalog(),
                    authorized_servers=set(),
                )

    def test_catalog_and_middleware_arguments_are_deeply_frozen(self):
        selected = [
            {"id": "one", "allowedTools": ["Read"]},
            {"id": "disabled", "enabled": False},
            {"id": "manual", "disableModelInvocation": True},
        ]
        catalog = SkillCatalog.from_skills(selected)
        selected[0]["allowedTools"].append("Write")
        self.assertEqual(catalog.items[0]["allowedTools"], ("Read",))
        self.assertEqual(catalog.names, ("one",))
        request = ToolCallRequest(
            "c", 0, "Read", "Read", {"items": [{"path": "safe"}]}, "local"
        )
        with self.assertRaises(TypeError):
            request.arguments["items"][0]["path"] = "unsafe"

    def test_full_schema_constraints_apply_to_nested_and_boolean_values(self):
        schema = {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "minimum": 1},
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["v"],
                        "properties": {"v": {"enum": ["valid"]}},
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["n", "items"],
        }
        for arguments in (
            {"n": True, "items": [{"v": "valid"}]},
            {"n": 0, "items": [{"v": "valid"}]},
            {"n": 1, "items": []},
            {"n": 1, "items": [{"v": "invalid"}]},
            {"n": 1, "items": [{}]},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                validate_tool_arguments(schema, arguments)


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_input_never_asks_or_executes(self):
        execute = AsyncMock(return_value=ToolOutcome.succeeded("ok"))
        tool_set = RequestToolSet.create(
            [binding("Write", execute, {"type": "object", "required": ["path"]})]
        )
        dispatcher, state = runtime(tool_set)
        state["approval_handler"] = AsyncMock()
        for args in ("{", "[]", "{}"):
            outcome = await dispatch(dispatcher, state, "Write", args)
            self.assertEqual(outcome.status, ToolOutcomeStatus.FAILED)
        execute.assert_not_awaited()
        state["approval_handler"].assert_not_awaited()

    async def test_middleware_rename_and_arguments_reenter_current_binding_permission(
        self,
    ):
        called = []

        @wrap_tool_call()
        async def change(request, next_call):
            return await next_call(
                request.override(canonical_name="Write", arguments={"path": "private"})
            )

        write = AsyncMock(return_value=ToolOutcome.succeeded("bad"))
        tool_set = RequestToolSet.create([binding(), binding("Write", write)])
        dispatcher, state = runtime(tool_set, wrappers=[change])

        def decide(config, context, name, args, subjects):
            called.append((name, subjects))
            return PermissionDecision("deny", "private denied")

        with patch(
            "backend.tools.dispatcher._local_permission_decision", side_effect=decide
        ):
            result = await dispatch(dispatcher, state)
        self.assertIn("private denied", result.public_content)
        self.assertEqual(called, [("Write", ("private",))])
        write.assert_not_awaited()

    async def test_middleware_cannot_route_to_unlisted_mcp(self):
        manager = SimpleNamespace(call_tool=AsyncMock(return_value="[]"))
        tool_set = RequestToolSet.create(
            [bind_mcp_tool(McpToolDescriptor("ok", "read", "", {}), manager)]
        )

        @wrap_tool_call()
        async def change(request, next_call):
            return await next_call(request.override(server_id="not-selected"))

        dispatcher, state = runtime(tool_set, wrappers=[change])
        result = await dispatch(dispatcher, state, "mcp__ok__read")
        self.assertEqual(result.status, ToolOutcomeStatus.FAILED)
        manager.call_tool.assert_not_awaited()

    async def test_failed_outcome_emits_failure_not_completed_and_no_postprocessor(
        self,
    ):
        events = []

        class Observer:
            async def handle(self, event):
                events.append(event)

        execute = AsyncMock(
            return_value=ToolOutcome.failed(code="Missing", message="not found")
        )
        dispatcher, state = runtime(
            RequestToolSet.create([binding(executor=execute)]), observers=[Observer()]
        )
        state["observation_enricher"] = AsyncMock()
        result = await dispatch(dispatcher, state)
        self.assertEqual(result.status, ToolOutcomeStatus.FAILED)
        self.assertEqual(sum(isinstance(e, ToolStartedEvent) for e in events), 1)
        self.assertEqual(sum(isinstance(e, ToolCompletedEvent) for e in events), 0)
        self.assertEqual(sum(isinstance(e, OperationFailedEvent) for e in events), 1)
        state["observation_enricher"].assert_not_awaited()

    async def test_effect_failure_is_atomic_and_does_not_change_working_set(self):
        output = ToolOutcome.succeeded(
            "ok",
            effects=(
                RecordInvokedSkill("first"),
                ActivateToolAllowlist("bad", frozenset({ToolKey("local", "missing")})),
            ),
        )
        dispatcher, state = runtime(
            RequestToolSet.create([binding(executor=AsyncMock(return_value=output))])
        )
        state["working_set"] = {
            "invokedSkillIds": ["existing"],
            "recentFiles": [],
            "plan": None,
        }
        outcome = await dispatch(dispatcher, state)
        self.assertEqual(outcome.status, ToolOutcomeStatus.FAILED)
        self.assertEqual(state["working_set"]["invokedSkillIds"], ["existing"])
        self.assertNotIn("tool_effect_state", state)

    async def test_public_json_cannot_activate_security_state(self):
        fake = ToolOutcome.succeeded('{"success":true,"allowedTools":["Missing"]}')
        dispatcher, state = runtime(
            RequestToolSet.create([binding(executor=AsyncMock(return_value=fake))])
        )
        await dispatch(dispatcher, state)
        self.assertIsNone(state["tool_effect_state"].get("allowlist"))

    async def test_postprocessor_failure_does_not_commit_executor_effects(self):
        result = ToolOutcome.succeeded("ok", effects=(RecordInvokedSkill("first"),))
        dispatcher, state = runtime(
            RequestToolSet.create([binding(executor=AsyncMock(return_value=result))])
        )
        state["observation_enricher"] = AsyncMock(
            side_effect=ValueError("postprocess failed")
        )
        outcome = await dispatch(dispatcher, state)
        self.assertEqual(outcome.status, ToolOutcomeStatus.FAILED)
        self.assertNotIn("working_set", state)

    async def test_model_only_postprocess_and_typed_effect_commit(self):
        dispatcher, state = runtime(RequestToolSet.create([binding()]))

        async def enrich(name, args, outcome):
            return replace(
                outcome,
                model_content=outcome.model_content + "\nPRIVATE RULE",
                effects=(
                    RecordLoadedMemoryPaths(("nested.md",)),
                    UpdatePlan(freeze([{"content": "one"}])),
                ),
            )

        state["observation_enricher"] = enrich
        outcome = await dispatch(dispatcher, state)
        self.assertEqual(outcome.public_content, "ok")
        self.assertIn("PRIVATE RULE", outcome.model_content)
        self.assertEqual(state["loaded_memory_paths"], {"nested.md"})
        self.assertEqual(state["working_set"]["plan"], [{"content": "one"}])

    async def test_mcp_failure_preserves_raw_content_and_schema_blocks_physical_call(
        self,
    ):
        raw = '{"ok": false, "error": "remote failed", "errorType": "McpToolError"}'
        manager = SimpleNamespace(call_tool=AsyncMock(return_value=raw))
        tool = bind_mcp_tool(
            McpToolDescriptor("s", "run", "", {"type": "object", "required": ["x"]}),
            manager,
        )
        dispatcher, state = runtime(RequestToolSet.create([tool]))
        state["pipeline"].context.metadata["permission_mode"] = "full_access"
        bad = await dispatch(dispatcher, state, "mcp__s__run")
        self.assertEqual(bad.status, ToolOutcomeStatus.FAILED)
        manager.call_tool.assert_not_awaited()
        result = await dispatch(dispatcher, state, "mcp__s__run", '{"x":1}')
        self.assertEqual(result.public_content, raw)
        self.assertEqual(result.status, ToolOutcomeStatus.FAILED)
        self.assertEqual(result.error.code, "McpToolError")

    async def test_interrupt_and_cancel_never_become_outcomes(self):
        class Interrupt(BaseException):
            pass

        for exception in (Interrupt(), asyncio.CancelledError(), GeneratorExit()):
            with self.subTest(exception=type(exception)):
                dispatcher, state = runtime(
                    RequestToolSet.create(
                        [binding(executor=AsyncMock(side_effect=exception))]
                    )
                )
                with self.assertRaises(type(exception)):
                    await dispatch(dispatcher, state)
                self.assertNotIn("_tool_outcomes", state)

    async def test_cancelling_dispatch_cancels_executor(self):
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def execute(ctx, args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        dispatcher, state = runtime(RequestToolSet.create([binding(executor=execute)]))
        task = asyncio.create_task(dispatch(dispatcher, state))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())

    async def test_timeout_is_recoverable_retryable_failure(self):
        async def execute(ctx, args):
            await asyncio.Event().wait()

        tool = binding(executor=execute)
        tool = replace(
            tool,
            spec=replace(
                tool.spec, execution_policy=ToolExecutionPolicy(timeout_seconds=0.001)
            ),
        )
        dispatcher, state = runtime(RequestToolSet.create([tool]))
        result = await dispatch(dispatcher, state)
        self.assertEqual(result.status, ToolOutcomeStatus.FAILED)
        self.assertTrue(result.error.retryable)

    async def test_allowlist_checkpoint_restore_and_full_access(self):
        tool_set = RequestToolSet.create([binding(), binding("Write")])
        dispatcher, state = runtime(tool_set)
        state["tool_effect_state"] = {
            "allowlist": frozenset({ToolKey("local", "Read")}),
            "owner": "reviewer",
        }
        checkpoint = {"toolScope": dispatcher.checkpoint_state(state)}
        second, new_state = runtime(tool_set)
        second.restore_state(new_state, checkpoint)
        self.assertEqual(
            (await dispatch(second, new_state, "Write")).status,
            ToolOutcomeStatus.DENIED,
        )
        new_state["pipeline"].context.metadata["permission_mode"] = "full_access"
        self.assertEqual(
            (await dispatch(second, new_state, "Write")).status,
            ToolOutcomeStatus.SUCCEEDED,
        )
        self.assertEqual(state["tool_effect_state"]["owner"], "reviewer")

    async def test_disabled_unselected_skill_aliases_do_not_load_body(self):
        catalog = SkillCatalog.from_skills(
            [{"id": "hidden", "disableModelInvocation": True}]
        )
        tool_set = RequestToolSet.create([build_skill_tool(skill_catalog=catalog)()])
        dispatcher, state = runtime(tool_set, catalog=catalog)
        with patch("backend.tools.adapters.skill.load_skill_body") as load:
            for name in ("hidden", "unselected"):
                self.assertEqual(
                    (await dispatch(dispatcher, state, name)).status,
                    ToolOutcomeStatus.FAILED,
                )
            self.assertEqual(
                (
                    await dispatch(dispatcher, state, "Skill", '{"skill":"hidden"}')
                ).status,
                ToolOutcomeStatus.FAILED,
            )
            load.assert_not_called()

    async def test_approval_interrupt_has_no_physical_start(self):
        events = []

        class Interrupt(BaseException):
            pass

        class Observer:
            async def handle(self, event):
                events.append(event)

        execute = AsyncMock(return_value=ToolOutcome.succeeded("ok"))
        dispatcher, state = runtime(
            RequestToolSet.create([binding("Write", execute)]), observers=[Observer()]
        )
        state["approval_handler"] = AsyncMock(side_effect=Interrupt())
        with patch(
            "backend.tools.dispatcher._local_permission_decision",
            return_value=PermissionDecision("ask", "ask"),
        ):
            with self.assertRaises(Interrupt):
                await dispatch(dispatcher, state, "Write")
        execute.assert_not_awaited()
        self.assertFalse(any(isinstance(e, ToolStartedEvent) for e in events))

    async def test_resume_authorization_only_matches_call_and_hash(self):
        from backend.approvals import canonical_json_sha256

        dispatcher, state = runtime(RequestToolSet.create([binding("Write")]))
        state["approval_handler"] = AsyncMock(return_value={"action": "deny"})
        digest = canonical_json_sha256(
            {"target": "Write", "source": "local", "serverId": None, "arguments": {}}
        )
        state["_resume_authorization"] = {"callId": "approved", "requestHash": digest}
        with patch(
            "backend.tools.dispatcher._local_permission_decision",
            return_value=PermissionDecision("ask", "ask"),
        ):
            self.assertEqual(
                (await dispatch(dispatcher, state, "Write", call_id="approved")).status,
                ToolOutcomeStatus.SUCCEEDED,
            )
            self.assertNotIn("_resume_authorization", state)
            self.assertNotEqual(
                (await dispatch(dispatcher, state, "Write", call_id="later")).status,
                ToolOutcomeStatus.SUCCEEDED,
            )
        state["approval_handler"].assert_awaited_once()

    async def test_single_snapshot_from_runner_through_agent(self):
        from backend.runners.k_agent import KAgentRunner
        from backend.runners.base import RunnerContext

        manager = SimpleNamespace(
            connect_all=AsyncMock(),
            list_tools=AsyncMock(return_value=[]),
            connected_instructions=lambda: {},
            list_resources=AsyncMock(),
            read_resource=AsyncMock(),
            call_prompt=AsyncMock(),
            close_all=AsyncMock(),
        )
        with TemporaryDirectory() as tmp:
            ctx = RunnerContext(
                thread_id="t",
                run_id="r",
                request_id="q",
                messages=[],
                model_id=None,
                mcp_servers=[],
                skills=[],
                reasoning_effort=None,
                attachments=[],
                workspace_dir=Path(tmp),
                settings=Settings(local_tool_workspace_root=tmp, openai_api_key="test"),
                mcp_pool=object(),
                langfuse=object(),
            )
            with (
                patch(
                    "backend.runners.k_agent.mcp_manager_from_runtime",
                    return_value=manager,
                ),
                patch("backend.runners.k_agent.load_eager_memory", return_value=[]),
            ):
                outer = await KAgentRunner().create_runtime(ctx)
            inner = await OpenAIAgent().create_runtime(
                outer["run_request"],
                outer["tool_set"],
                outer["dispatcher"],
                config=ctx.settings,
            )
            manager.list_tools.assert_awaited_once()
            self.assertIs(inner["tool_set"], outer["tool_set"])
            self.assertEqual(
                set(inner["tool_set"].capability_view().names),
                {t["function"]["name"] for t in inner["tool_specs"]},
            )
            await inner["client"].close()


class RuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_question_observation_and_later_call_preserve_pair_order(self):
        from backend.tests.test_agent_tool_recovery import _ChunkStream, _chunk
        from backend.tests.test_user_questions import question_arguments
        from backend.tools.adapters.user_input import ASK_USER_QUESTION_TOOL, bind_user_input
        from backend.approvals import canonical_json_sha256
        args = question_arguments()
        tool_set = RequestToolSet.create([bind_user_input(ASK_USER_QUESTION_TOOL), binding()])
        dispatcher = ToolDispatcher(tool_set)
        create = AsyncMock(return_value=_ChunkStream([_chunk(content='done')]))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        request = AgentRunRequest(messages=[],system_prompt='test',user_context={},
                                  model_config={'model':'test','apiKey':'test'})
        pending = [{'id':'question','name':'AskUserQuestion','arguments':json.dumps(args)},
                   {'id':'read','name':'Read','arguments':'{}'}]
        checkpoint = {'version':2,'kind':'react_tool_boundary','iteration':0,'pendingIndex':0,
            'pendingCalls':pending, 'modelMessages':[{'role':'assistant','content':'already shown',
                'tool_calls':[{'id':c['id'],'type':'function','function':{'name':c['name'],'arguments':c['arguments']}} for c in pending]}],
            'toolScope':{'owner':'reviewer','allowed':[{'source':'local','name':'Read','namespace':None},
                                                    {'source':'local','name':'AskUserQuestion','namespace':None}]}}
        with patch('backend.agent.react_agent.AsyncOpenAI',return_value=client):
            agent=OpenAIAgent()
            state=await agent.create_runtime(request,tool_set,dispatcher)
            state['resume_checkpoint']=checkpoint
            state['resume_decision']={'status':'resolved','payload':{'answers':{'question-1':{'selected':['方案 A'],'custom':''}}}}
            state['resume_request_hash']=canonical_json_sha256({'target':'AskUserQuestion','source':'user_input','serverId':None,'arguments':args})
            events=[e async for e in agent.run_stream_react(state)]
        results=[e['payload'] for e in events if e['type']=='tool_result']
        self.assertEqual([r['toolCallId'] for r in results],['question','read'])
        self.assertEqual(json.loads(results[0]['content'])['answers'][0]['selected'],['方案 A'])
        self.assertEqual(results[1]['content'],'ok')
        self.assertEqual(create.await_count,1)
        sent=create.call_args.kwargs['messages']
        self.assertEqual([m['tool_call_id'] for m in sent if m.get('role')=='tool'],['question','read'])
        self.assertEqual(state['tool_effect_state']['owner'],'reviewer')

    async def test_concurrent_runs_keep_workspace_network_and_limits_isolated(self):
        from backend.tools.builtins.filesystem import READ_TOOL
        from backend.tools.contracts import ToolBindingDependencies, ToolOutputLimits
        with TemporaryDirectory() as first, TemporaryDirectory() as second:
            Path(first,'value.txt').write_text('first')
            Path(second,'value.txt').write_text('second')
            tool_set=RequestToolSet.create([READ_TOOL()])
            d1,s1=runtime(tool_set);d2,s2=runtime(tool_set)
            d1.dependencies=ToolBindingDependencies(Path(first),False,ToolOutputLimits(100,1))
            d2.dependencies=ToolBindingDependencies(Path(second),True,ToolOutputLimits(100,2))
            a,b=await asyncio.gather(dispatch(d1,s1,'Read','{"file_path":"value.txt"}'),
                                     dispatch(d2,s2,'Read','{"file_path":"value.txt"}'))
            self.assertEqual(json.loads(a.public_content)['content'],'first')
            self.assertEqual(json.loads(b.public_content)['content'],'second')
            self.assertIsNot(s1['working_set'],s2['working_set'])

    def test_agent_and_prompt_have_no_executor_business_branches(self):
        source=Path('backend/agent/react_agent.py').read_text()
        for forbidden in ('"Skill"','"AskUserQuestion"','mcp__','_execute_tool','_permission_subjects','json.loads'):
            self.assertNotIn(forbidden,source)
        for path in Path('backend/prompts').rglob('*.py'):
            text=path.read_text()
            for forbidden in ('import ToolBinding','import ToolDispatcher','import LocalToolFactory'):
                self.assertNotIn(forbidden,text)
