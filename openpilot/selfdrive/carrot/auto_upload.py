#!/usr/bin/env python3
"""Background upload of finished drive logs to the owner's own Carrot upload server.

Runs only when Carrot Web's upload server is changed from the public default, so
logs never reach a shared server without an explicit choice. Finished segments
(no *.lock, idle for a while) are sent oldest-first at the lowest CPU priority:
qlog always, rlog only when /data/carrot_auto_upload_rlog exists. Failures back
off and retry; driving processes never wait on this daemon.
"""
import asyncio
import json
import os
import re
import time

from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.carrot.server.config import DASHCAM_ROOT
from openpilot.selfdrive.carrot.server.features.dashcam.catalog import QLOG_SOURCE_NAMES, RLOG_SOURCE_NAMES
from openpilot.selfdrive.carrot.server.features.dashcam import upload
from openpilot.selfdrive.carrot.web_upload import (
  DEFAULT_WEB_UPLOAD_URL, create_web_upload_session, normalize_base_url, upload_device_id, upload_folder_to_web,
)

STATE_PATH = "/data/carrot_auto_upload.json"
RLOG_FLAG_PATH = "/data/carrot_auto_upload_rlog"
SEGMENT_RE = re.compile(r"^[0-9A-Za-z-]+--[0-9A-Za-z-]+--\d+$")
SCAN_INTERVAL_S = 60
SEGMENT_IDLE_S = 90             # a segment still being finalized is skipped
MAX_SEGMENT_AGE_S = 3 * 86400   # do not backfill old history on first enable
FILE_PAUSE_S = 2                # keep the radio and CPU free between files
MAX_BACKOFF_S = 600


def load_state() -> dict[str, list[str]]:
  try:
    with open(STATE_PATH, encoding="utf-8") as f:
      data = json.load(f)
    return {str(k): [str(n) for n in v] for k, v in data.items()} if isinstance(data, dict) else {}
  except Exception:
    return {}


def save_state(state: dict[str, list[str]]) -> None:
  tmp = STATE_PATH + ".tmp"
  with open(tmp, "w", encoding="utf-8") as f:
    json.dump(state, f, sort_keys=True)
  os.replace(tmp, STATE_PATH)


def pick(folder: str, names: tuple[str, ...]) -> str | None:
  for name in names:
    path = os.path.join(folder, name)
    if os.path.isfile(path) and os.path.getsize(path) > 0:
      return name
  return None


def pending_files(state: dict[str, list[str]], include_rlog: bool, now: float) -> list[tuple[str, list[str]]]:
  try:
    entries = [e for e in os.scandir(DASHCAM_ROOT) if e.is_dir(follow_symlinks=False) and SEGMENT_RE.match(e.name)]
  except OSError:
    return []
  out = []
  for entry in sorted(entries, key=lambda e: e.stat().st_mtime):
    try:
      names = os.listdir(entry.path)
      mtime = max([entry.stat().st_mtime] + [os.path.getmtime(os.path.join(entry.path, n)) for n in names])
    except OSError:
      continue
    if any(n.endswith(".lock") for n in names) or now - mtime < SEGMENT_IDLE_S or now - mtime > MAX_SEGMENT_AGE_S:
      continue
    done = set(state.get(entry.name, []))
    wanted = [pick(entry.path, QLOG_SOURCE_NAMES)] + ([pick(entry.path, RLOG_SOURCE_NAMES)] if include_rlog else [])
    todo = [n for n in wanted if n and n not in done]
    if todo:
      out.append((entry.name, todo))
  return out


def upload_target() -> tuple[str, str] | None:
  base_url, token = upload.upload_target_settings()
  base_url = normalize_base_url(base_url)
  if not base_url or base_url == normalize_base_url(DEFAULT_WEB_UPLOAD_URL):
    return None
  return base_url, token


async def upload_pending(params: Params, state: dict[str, list[str]]) -> int:
  target = upload_target()
  if target is None:
    return 0
  work = pending_files(state, os.path.exists(RLOG_FLAG_PATH), time.time())
  if not work:
    return 0
  base_url, token = target
  meta = upload.upload_metadata(params)
  device_id = upload_device_id(meta)
  if not token:
    token = await create_web_upload_session(base_url, meta, "dashcam")
  sent = 0
  for segment, names in work:
    for name in names:
      await upload_folder_to_web(os.path.join(DASHCAM_ROOT, segment), device_id, segment, base_url, token, filenames=[name])
      state.setdefault(segment, []).append(name)
      save_state(state)
      sent += 1
      await asyncio.sleep(FILE_PAUSE_S)
  return sent


def prune(state: dict[str, list[str]]) -> None:
  for segment in list(state):
    if not os.path.isdir(os.path.join(DASHCAM_ROOT, segment)):
      del state[segment]


def main() -> None:
  try:
    os.nice(19)
  except OSError:
    pass
  params = Params()
  state = load_state()
  prune(state)
  backoff = SCAN_INTERVAL_S
  while True:
    try:
      sent = asyncio.run(upload_pending(params, state))
      if sent:
        cloudlog.info(f"carrot auto upload: sent {sent} files")
      backoff = SCAN_INTERVAL_S
    except Exception as e:
      cloudlog.warning(f"carrot auto upload failed, retry in {backoff}s: {e}")
      backoff = min(MAX_BACKOFF_S, backoff * 2)
    time.sleep(backoff)


if __name__ == "__main__":
  main()
