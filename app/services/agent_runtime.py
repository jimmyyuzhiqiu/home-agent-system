from pathlib import Path
from threading import Thread
from types import SimpleNamespace

from extensions import db
from models import Conversation, Message, TaskRunAudit, User
from services.bridge_client import bridge_agent_turn, bridge_deliver_bluebubbles, build_upload_ref, resolve_bridge_state
from services.conversations import ensure_user_agent_binding, touch_conversation
from services.memory import ensure_system_memories, get_user_memories
from services.runtime_records import add_run_artifact, add_run_event, record_delivery, serialize_run_artifacts, serialize_run_events
from services.uploads import save_generated_artifact
from settings import get_env, get_public_base_url
from utils import contains_permission_block, normalize_url, parse_plan_steps, render_markdown, summarize_text, utcnow


STATUS_LABELS = {
    "queued": "等待队列",
    "running": "进行中",
    "pending": "待开始",
    "done": "已完成",
    "failed": "失败",
    "blocked": "阻塞",
    "skipped": "未发送",
}


def resolve_gateway(_user=None):
    return resolve_bridge_state()


def inject_memory_context(user_id: int, text: str) -> str:
    memories = get_user_memories(user_id, limit=8)
    if not memories:
        return text
    lines = [f"- ({item.kind}) {item.content}" for item in memories]
    prefix = "[长期记忆(仅当前用户)]\n" + "\n".join(lines) + "\n[/长期记忆]\n\n"
    return prefix + text


def _update_run(run: TaskRunAudit, **fields):
    for key, value in fields.items():
        setattr(run, key, value)
    db.session.commit()


def _stage_copy(stage: str) -> tuple[str, str]:
    if stage == "planner":
        return "需求拆解", "正在拆解需求"
    if stage == "worker":
        return "执行处理中", "正在执行任务"
    if stage == "verify":
        return "结果校验中", "正在整理并校验结果"
    return stage, "正在处理任务"


def _public_error(message: str, default: str) -> str:
    raw = (message or "").strip()
    if not raw:
        return default
    lowered = raw.lower()
    if "403" in lowered or "forbidden" in lowered:
        return "当前执行请求被拒绝，请联系管理员检查 Runtime 权限配置。"
    if "404" in lowered or "not found" in lowered:
        return "运行时请求的资源不存在，请联系管理员检查容器内 OpenClaw 配置。"
    if "timed out" in lowered or "timeout" in lowered:
        return "执行超时，请稍后重试。"
    if "failed to establish a new connection" in lowered or "connection refused" in lowered:
        return "执行后端暂时不可用，请联系管理员检查容器内 Runtime。"
    return default


def _store_artifacts(user: User, run: TaskRunAudit, artifacts: list[dict], message_id: int | None = None):
    for item in artifacts:
        file_path = item.get("file_path")
        mime_type = item.get("mime_type")
        if item.get("inline_text"):
            saved = save_generated_artifact(
                user,
                item.get("filename") or item.get("title") or "result.md",
                item.get("inline_text") or "",
                suffix=Path(item.get("filename") or "").suffix or None,
            )
            file_path = saved["attachment_path"]
            mime_type = mime_type or saved.get("mime_type")
        add_run_artifact(
            run.id,
            kind=item.get("kind") or "output_file",
            title=item.get("title") or "执行产物",
            summary=item.get("summary") or "",
            source_url=item.get("source_url"),
            file_path=file_path,
            mime_type=mime_type,
            preview_image_path=item.get("preview_image_path"),
            visibility=item.get("visibility") or "user",
            message_id=message_id if (item.get("visibility") or "user") == "user" else None,
            meta=item.get("meta") or {},
        )


def _record_bridge_trace(run: TaskRunAudit, user: User, bridge_payload: dict, stage: str, message_id: int | None = None):
    tool_events = bridge_payload.get("tool_events") or []
    artifacts = bridge_payload.get("artifacts") or []
    for event in tool_events:
        add_run_event(
            run.id,
            stage=event.get("stage") or stage,
            event_type=event.get("event_type") or stage,
            status=event.get("status") or "done",
            label=event.get("label") or stage,
            public_text=event.get("public_text") or "",
            admin_text=event.get("admin_text") or "",
            progress_percent=event.get("progress_percent"),
            meta=event.get("meta") or {},
        )
    if bridge_payload.get("meta", {}).get("fetched_attachments"):
        add_run_event(
            run.id,
            stage=stage,
            event_type="attachment_fetch",
            status="done",
            label="已同步附件",
            public_text="已接收附件并准备处理",
            admin_text=str(bridge_payload.get("meta", {}).get("fetched_attachments")),
        )
    _store_artifacts(user, run, artifacts, message_id=message_id)
    run.bridge_run_id = bridge_payload.get("bridge_run_id") or run.bridge_run_id
    run.tool_trace_count = max(run.tool_trace_count or 0, len(tool_events) + len(artifacts))
    db.session.commit()


def _bridge_stage_turn(
    *,
    user: User,
    binding,
    agent: SimpleNamespace,
    stage: str,
    run: TaskRunAudit,
    message_text: str,
    attachment_refs: list[dict] | None = None,
):
    title, public_copy = _stage_copy(stage)
    add_run_event(
        run.id,
        stage=stage,
        event_type=f"{stage}_started",
        status="running",
        label=title,
        public_text=public_copy,
        admin_text=f"{stage} agent={agent.agent_id} session={agent.session_key}",
        progress_percent=run.progress_percent,
    )
    payload = {
        "user_id": user.id,
        "namespace": binding.bridge_namespace or user.memory_namespace,
        "execution_profile": user.execution_profile,
        "agent_role": stage,
        "agent_id": agent.agent_id,
        "session_key": agent.session_key,
        "workspace": agent.workspace,
        "model": agent.model,
        "message": message_text,
        "attachment_refs": attachment_refs or [],
        "meta": {"username": user.username, "conversation_id": run.conversation_id, "run_id": run.id},
    }
    try:
        result = bridge_agent_turn(payload)
    except Exception as exc:
        technical = str(exc)
        return {
            "ok": False,
            "public_error": _public_error(technical, "执行后端暂时不可用，请联系管理员检查容器内 Runtime。"),
            "technical_error": technical,
        }
    if not result.get("ok"):
        technical = result.get("error") or "bridge returned failure"
        return {
            "ok": False,
            "public_error": _public_error(technical, "执行失败，请稍后重试。"),
            "technical_error": technical,
        }
    add_run_event(
        run.id,
        stage=stage,
        event_type=f"{stage}_finished",
        status="done",
        label=title,
        public_text=f"{title}已完成",
        admin_text=result.get("raw_ref") or "",
        progress_percent=run.progress_percent,
    )
    return result


def run_dual_agent_cycle(user: User, conversation: Conversation, binding, user_text: str, attachment_refs: list[dict] | None = None, run: TaskRunAudit | None = None):
    started = utcnow()
    planner = SimpleNamespace(
        agent_id=binding.planner_agent_id or binding.agent_id,
        session_key=conversation.session_key or binding.planner_session_key or binding.session_key,
        model=binding.model,
        workspace=binding.planner_workspace,
    )
    worker = SimpleNamespace(
        agent_id=binding.worker_agent_id or binding.agent_id,
        session_key=conversation.worker_session_key or binding.worker_session_key,
        model=binding.model,
        workspace=binding.worker_workspace,
    )

    if run:
        _update_run(
            run,
            status="running",
            public_status_label="需求拆解中",
            current_stage="planner",
            progress_percent=12,
            planner_status="running",
            worker_status="pending",
            verify_status="pending",
        )

    planner_prompt = (
        "你是 planner。将用户需求拆解为最多 5 条可执行步骤，输出中文条目。"
        "除权限阻塞外不要要求用户补充信息。\n\n"
        f"用户需求：{user_text}"
    )
    planner_res = _bridge_stage_turn(
        user=user,
        binding=binding,
        agent=planner,
        stage="planner",
        run=run,
        message_text=inject_memory_context(user.id, planner_prompt),
        attachment_refs=attachment_refs,
    )
    if not planner_res.get("ok"):
        return {
            "ok": False,
            "status": "failed",
            "stage": "planner",
            "public_error": planner_res.get("public_error"),
            "technical_error": planner_res.get("technical_error"),
        }
    plan_text = planner_res.get("reply_text", "")
    if run:
        _record_bridge_trace(run, user, planner_res, "planner")
        _update_run(
            run,
            planner_plan=plan_text[:4000],
            planner_status="done",
            public_status_label="执行处理中",
            current_stage="worker",
            progress_percent=38,
            worker_status="running",
        )

    worker_prompt = (
        "你是 worker。根据以下计划直接执行并产出结果。"
        "如果遇到权限阻塞，明确标注“权限阻塞”。否则直接给出成品结果。\n\n"
        f"计划：\n{plan_text}\n\n用户原始需求：{user_text}"
    )
    worker_res = _bridge_stage_turn(
        user=user,
        binding=binding,
        agent=worker,
        stage="worker",
        run=run,
        message_text=inject_memory_context(user.id, worker_prompt),
        attachment_refs=attachment_refs,
    )
    if not worker_res.get("ok"):
        return {
            "ok": False,
            "status": "failed",
            "stage": "worker",
            "public_error": worker_res.get("public_error"),
            "technical_error": worker_res.get("technical_error"),
            "plan_text": plan_text,
        }
    worker_text = worker_res.get("reply_text", "")
    worker_status = "blocked" if contains_permission_block(worker_text) else "done"
    if run:
        _record_bridge_trace(run, user, worker_res, "worker")
        _update_run(
            run,
            worker_output=worker_text[:4000],
            worker_status=worker_status,
            public_status_label="结果校验中",
            current_stage="verify",
            progress_percent=76,
            verify_status="running",
        )

    verify_prompt = (
        "你是 planner。请验证 worker 结果是否完成需求，并输出最终交付。"
        "格式：\n1) 完成状态\n2) 最终结果\n3) 如有阻塞仅列权限问题。\n\n"
        f"用户需求：{user_text}\n\n计划：{plan_text}\n\nworker 结果：{worker_text}"
    )
    final_res = _bridge_stage_turn(
        user=user,
        binding=binding,
        agent=planner,
        stage="verify",
        run=run,
        message_text=inject_memory_context(user.id, verify_prompt),
    )
    duration_ms = int((utcnow() - started).total_seconds() * 1000)
    if not final_res.get("ok"):
        return {
            "ok": False,
            "status": "failed",
            "stage": "verify",
            "public_error": final_res.get("public_error"),
            "technical_error": final_res.get("technical_error"),
            "plan_text": plan_text,
            "worker_text": worker_text,
            "duration_ms": duration_ms,
        }
    final_text = final_res.get("reply_text", "")
    final_status = "blocked" if contains_permission_block(worker_text, final_text) else "done"
    return {
        "ok": True,
        "status": final_status,
        "stage": "complete",
        "planner": planner_res,
        "worker": worker_res,
        "final": final_res,
        "plan_text": plan_text,
        "worker_text": worker_text,
        "final_text": final_text,
        "duration_ms": duration_ms,
    }


def _store_final_message(run: TaskRunAudit, conversation: Conversation, user: User, content: str, provider: str, render_mode: str = "result"):
    assistant_message = Message(
        conversation_id=conversation.id,
        user_id=user.id,
        role="assistant",
        content=content,
        render_mode=render_mode,
    )
    db.session.add(assistant_message)
    db.session.flush()
    run.assistant_message_id = assistant_message.id
    touch_conversation(conversation, content, provider=provider, role="assistant", has_attachment=False)
    return assistant_message


def _maybe_deliver_result(user: User, run: TaskRunAudit, conversation: Conversation, final_text: str):
    if get_env("HOME_AGENT_ENABLE_BLUEBUBBLES", "0").lower() not in {"1", "true", "yes", "on"}:
        run.delivery_status = "disabled"
        db.session.commit()
        return None
    recipient = (user.bluebubbles_recipient or "").strip()
    if not user.bluebubbles_enabled or not recipient:
        run.delivery_status = "skipped"
        db.session.commit()
        return None
    add_run_event(
        run.id,
        stage="delivery",
        event_type="delivery_started",
        status="running",
        label="结果回发",
        public_text="正在发送到绑定手机号",
        admin_text=f"bluebubbles -> {recipient}",
    )
    result_url = f"{normalize_url(get_public_base_url())}/chat?conversation={conversation.id}"
    delivery_text = "\n".join(
        [
            "任务完成" if run.status == "done" else "任务结果已返回",
            summarize_text(final_text or run.final_summary or "", 120),
            f"详情：{result_url}",
        ]
    )
    try:
        response = bridge_deliver_bluebubbles(
            {
                "user_id": user.id,
                "run_id": run.id,
                "recipient": recipient,
                "text": delivery_text,
                "summary": summarize_text(final_text or "", 120),
                "result_url": result_url,
            }
        )
    except Exception as exc:
        message = str(exc)
        delivery = record_delivery(
            user.id,
            run.id,
            channel="bluebubbles",
            recipient=recipient,
            status="failed",
            message_preview=delivery_text,
            error_message=message,
        )
        add_run_event(
            run.id,
            stage="delivery",
            event_type="delivery_failed",
            status="failed",
            label="结果回发失败",
            public_text="结果已生成，但短信回发失败",
            admin_text=message,
        )
        conversation.delivery_channel = "bluebubbles"
        conversation.delivery_recipient = recipient
        conversation.last_delivery_status = delivery.status
        db.session.commit()
        return delivery

    if response.get("status") == "disabled":
        run.delivery_status = "disabled"
        db.session.commit()
        return None
    status = response.get("status") if response.get("ok") else "failed"
    delivery = record_delivery(
        user.id,
        run.id,
        channel="bluebubbles",
        recipient=recipient,
        status=status,
        message_preview=delivery_text,
        error_message=response.get("error") or "",
        provider_ref=response.get("provider_ref") or "",
    )
    add_run_event(
        run.id,
        stage="delivery",
        event_type="delivery_done" if status == "done" else "delivery_failed",
        status="done" if status == "done" else "failed",
        label="结果已回发" if status == "done" else "结果回发失败",
        public_text="结果已发送到绑定手机号" if status == "done" else "结果已生成，但短信回发失败",
        admin_text=response.get("provider_ref") or response.get("error") or "",
    )
    conversation.delivery_channel = "bluebubbles"
    conversation.delivery_recipient = recipient
    conversation.last_delivery_status = delivery.status
    db.session.commit()
    return delivery


def execute_run(run_id: int):
    run = db.session.get(TaskRunAudit, run_id)
    if not run:
        return None
    user = db.session.get(User, run.user_id)
    conversation = db.session.get(Conversation, run.conversation_id)
    binding = ensure_user_agent_binding(user.id)
    ensure_system_memories(user.id)
    backend = resolve_bridge_state()
    if not backend.get("chat_ready"):
        run.status = "failed"
        run.current_stage = "planner"
        run.progress_percent = 0
        run.public_status_label = "执行后端未就绪"
        run.public_error_message = "执行后端未就绪，请联系管理员检查容器内 Runtime 与 Provider 配置。"
        run.technical_error_message = backend.get("detail") or backend.get("reason")
        run.error_message = run.technical_error_message
        run.planner_status = "failed"
        assistant_message = _store_final_message(run, conversation, user, run.public_error_message, "runtime:offline", render_mode="system")
        run.assistant_message_id = assistant_message.id
        run.final_summary = run.public_error_message
        db.session.commit()
        return run

    user_message = db.session.get(Message, run.user_message_id) if run.user_message_id else None
    attachment_refs = []
    if user_message and user_message.attachment_path:
        ref = build_upload_ref(user_message)
        if ref:
            attachment_refs.append(ref)

    result = run_dual_agent_cycle(user, conversation, binding, run.user_message, attachment_refs=attachment_refs, run=run)
    if not result.get("ok"):
        run.status = "failed"
        run.current_stage = result.get("stage", "failed")
        run.progress_percent = min(run.progress_percent or 0, 88)
        run.public_status_label = "执行失败"
        run.public_error_message = result.get("public_error") or "任务执行失败"
        run.technical_error_message = result.get("technical_error") or run.public_error_message
        run.error_message = run.technical_error_message
        if run.current_stage == "planner":
            run.planner_status = "failed"
        elif run.current_stage == "worker":
            run.worker_status = "failed"
        else:
            run.verify_status = "failed"
        content = run.public_error_message
        assistant_message = _store_final_message(run, conversation, user, content, "runtime:failed", render_mode="system")
        run.assistant_message_id = assistant_message.id
        run.final_summary = content[:4000]
        db.session.commit()
        return run

    provider_chain = "runtime:{planner}->{worker}->{final}".format(
        planner=result["planner"].get("provider"),
        worker=result["worker"].get("provider"),
        final=result["final"].get("provider"),
    )
    run.status = result["status"]
    run.current_stage = "complete"
    run.progress_percent = 100
    run.error_message = None
    run.public_error_message = None
    run.technical_error_message = None
    run.planner_plan = result["plan_text"][:4000]
    run.worker_output = result["worker_text"][:4000]
    run.final_summary = result["final_text"][:4000]
    run.duration_ms = result["duration_ms"]
    run.public_status_label = "已完成" if result["status"] == "done" else "结果存在阻塞"
    run.planner_status = "done"
    run.worker_status = "blocked" if result["status"] == "blocked" else "done"
    run.verify_status = "blocked" if result["status"] == "blocked" else "done"

    assistant_message = _store_final_message(run, conversation, user, result["final_text"], provider_chain)
    run.assistant_message_id = assistant_message.id
    _record_bridge_trace(run, user, result["final"], "verify", message_id=assistant_message.id)
    _maybe_deliver_result(user, run, conversation, result["final_text"])

    binding.agent_id = binding.planner_agent_id or binding.agent_id
    binding.session_key = conversation.session_key or binding.planner_session_key or binding.session_key
    binding.model = result["final"].get("model") or binding.model
    binding.last_provider = provider_chain
    binding.last_called_at = utcnow()
    binding.last_bridge_status = "done"
    binding.last_bridge_at = utcnow()

    conversation.agent_id = binding.agent_id
    conversation.model = binding.model
    conversation.last_provider = provider_chain
    conversation.last_called_at = binding.last_called_at
    db.session.commit()
    return run


def _run_in_thread(app, run_id: int):
    with app.app_context():
        execute_run(run_id)


def start_async_run(app, run_id: int):
    thread = Thread(target=_run_in_thread, args=(app, run_id), daemon=True)
    thread.start()
    return thread


def build_timeline(run: TaskRunAudit | None):
    run_status = (run.status if run else "pending") or "pending"
    current_stage = (run.current_stage if run else "queued") or "queued"
    items = [
        {
            "key": "planner",
            "title": "需求拆解",
            "status": (run.planner_status if run else "pending") or ("running" if current_stage == "planner" else "pending"),
            "summary": summarize_text(run.planner_plan if run else "", 76),
        },
        {
            "key": "worker",
            "title": "执行产出",
            "status": (run.worker_status if run else "pending") or ("running" if current_stage == "worker" else "pending"),
            "summary": summarize_text(run.worker_output if run else "", 76),
        },
        {
            "key": "verify",
            "title": "校验交付",
            "status": (run.verify_status if run else "pending") or ("running" if current_stage == "verify" else "pending"),
            "summary": summarize_text((run.final_summary or run.public_error_message or run.error_message) if run else "", 76),
        },
    ]
    for item in items:
        item["status_label"] = STATUS_LABELS.get(item["status"], item["status"])
    return {
        "overall": run_status,
        "overall_label": STATUS_LABELS.get(run_status, run_status),
        "progress_percent": run.progress_percent if run else 0,
        "current_stage": current_stage,
        "items": items,
        "plan_steps": parse_plan_steps(run.planner_plan if run else ""),
        "error_message": (run.public_error_message or run.error_message) if run else "",
    }


def build_run_status_payload(run: TaskRunAudit):
    timeline = build_timeline(run)
    assistant_message = db.session.get(Message, run.assistant_message_id) if run.assistant_message_id else None
    final_text = assistant_message.content if assistant_message else (run.final_summary or "")
    return {
        "ok": True,
        "run_id": run.id,
        "status": run.status,
        "public_status_label": run.public_status_label or STATUS_LABELS.get(run.status, run.status or "空闲"),
        "current_stage": run.current_stage,
        "progress_percent": run.progress_percent or 0,
        "planner_plan": run.planner_plan or "",
        "worker_excerpt": summarize_text(run.worker_output or "", 180),
        "final_text": final_text,
        "final_html": str(render_markdown(final_text)),
        "error_message": run.public_error_message or run.error_message or "",
        "technical_error_message": run.technical_error_message or "",
        "assistant_message_id": run.assistant_message_id,
        "planner_status": run.planner_status,
        "worker_status": run.worker_status,
        "verify_status": run.verify_status,
        "timeline": timeline,
        "duration_ms": run.duration_ms,
        "public_events": serialize_run_events(run.id, visibility="user"),
        "admin_events": serialize_run_events(run.id, visibility="admin"),
        "user_artifacts": serialize_run_artifacts(run.id, visibility="user"),
        "admin_artifacts": serialize_run_artifacts(run.id, visibility="admin"),
        "delivery_status": run.delivery_status,
    }
