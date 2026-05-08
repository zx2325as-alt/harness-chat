from pathlib import Path

p = Path(__file__).parent / "harness.py"
lines = p.read_text(encoding="utf-8").splitlines(keepends=True)

stream_start = None
stream_end = None
sync_start = None
sync_end = None

for i, ln in enumerate(lines):
    if ln.strip() == "skip_draft = self._should_skip_refine_draft(prompt, analysis, options)":
        if i + 1 < len(lines) and "Layer 1 — 流式输出草稿" in lines[i + 1]:
            stream_start = i
        if (
            i + 2 < len(lines)
            and lines[i + 1].strip() == "# Layer 1"
            and "对于 refine 链的第一层" in lines[i + 2]
        ):
            sync_start = i

if stream_start is None or sync_start is None:
    raise SystemExit(f"markers missing stream_start={stream_start} sync_start={sync_start}")
if stream_start >= sync_start:
    raise SystemExit("expected run_stream refine before sync refine")

for j in range(stream_start, sync_start):
    if 'yield {"event": "step", "step": step_l3.to_dict()}' in lines[j]:
        stream_end = j + 1
        break
if stream_end is None:
    raise SystemExit("stream_end not found")

for j in range(sync_start, len(lines)):
    if '"final": r3.to_dict()' in lines[j]:
        for k in range(j, min(j + 12, len(lines))):
            if lines[k].strip() == '"meta": sync_meta,' and k + 1 < len(lines) and lines[k + 1].strip() == "}":
                sync_end = k + 3
                break
        if sync_end:
            break

if sync_end is None:
    raise SystemExit("sync_end not found")

stream_inject = (
    "        skip_draft = self._should_skip_refine_draft(prompt, analysis, options)\n"
    "        async for ev in iter_refine_runtime_stream(\n"
    "            self,\n"
    "            prompt,\n"
    "            analysis,\n"
    "            options,\n"
    "            messages,\n"
    "            trace_id,\n"
    "            hcfg,\n"
    "            _tag,\n"
    "            entry_block=entry_block,\n"
    "            skip_draft=skip_draft,\n"
    "        ):\n"
    "            yield ev\n"
    "        return\n"
    "\n"
)

sync_inject = (
    "        skip_draft = self._should_skip_refine_draft(prompt, analysis, options)\n"
    "        res_rf, extra_rf = await self._consume_refine_runtime_full_sync(\n"
    "            prompt,\n"
    "            analysis,\n"
    "            options,\n"
    "            messages,\n"
    "            trace_id,\n"
    "            hcfg,\n"
    "            _tag,\n"
    "            entry_block=entry_block,\n"
    "            skip_draft=skip_draft,\n"
    "        )\n"
    "        steps.extend(extra_rf)\n"
    "        return {\n"
    '            "trace_id": trace_id,\n'
    '            "track": "refine",\n'
    '            "final": res_rf.to_dict(),\n'
    '            "steps": [s.to_dict() for s in steps],\n'
    '            "meta": sync_meta,\n'
    "        }\n"
    "\n"
)

out = lines[:stream_start] + [stream_inject] + lines[stream_end:sync_start] + [sync_inject] + lines[sync_end:]
p.write_text("".join(out), encoding="utf-8")
print("patched stream", stream_start + 1, stream_end, "sync", sync_start + 1, sync_end)
