# -*- coding: utf-8 -*-
"""dsm.py — DSM v1（Delta Session Mesh）SMSocket 侧编解码器（服务端）。

真源：Downloads/OpenAI兼容格式评测_20261005/DSM_v1/{规范_DSM_v1.md,dsm.schema.json}
契约：DSM_wiring_contract.md §1-§6（端点/键名/开关语义/验收矩阵），逐条遵守、不自创。
§3 硬要求：本文件的共享算法（ROLE/RID、POLICY_KEYS、CORE、validate、fingerprint、
canon_mem/canon_cons、budget_map、to_openai/to_anthropic/to_responses、split_fan、
leak_check、name_collision、encode_turn/decode_turn、decode_to_openai_shape、
merge_stream）与 SMS 侧 skill_manage_system/skill/scripts/dsm.py 逐字节同构——
由 tools/gen_server_dsm.py 从客户端文件机械抽取拼装，不手抄、不重发明；客户端一旦
改名/改语义，生成器直接 ABORT，两侧不可能悄悄漂移。
服务端差异只在三处：
  · frag/canon_mem —— 链库在 SMS 本机，服务端读不到：恒 None 并计 mem_unsupported，
    绝不静默当成「没有记忆」；
  · SchemaStore —— 服务端才是引用真源，落 <data>/runtime/dsm_schemas.json（原子写）；
  · SessionStore + encode_response —— 服务端持有会话态（d 只带新增轮次）并产出三分账
    响应信封（in/out_reason/out_answer/cache_read/cache_write/cost）。
默认全关：settings.dsm.enabled=false 时本模块不被任何路由调用，行为与今天逐字节相同（验收 A）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

log = logging.getLogger("smssocket.dsm")

# ---- server-side constants the shared codec references (see module docstring) ----
# 链在服务端不存在（链库在 SMS 本机），故 ISO 过滤集为空：_same_session 因此恒真，
# 而 frag() 恒返回 None —— 服务端 mem 一律解析不到，由 mem_unsupported 计数暴露。
ISO_CHAINS: tuple = ()
RESPONSES_STYLE = "openai-responses"      # 与 SMSocket/upstream.py 同值（出口 style 分派）
LANE_CORE_TOOLS = ("exec", "read", "write", "grep", "glob", "ls")

_mem_unsupported = 0


def frag(fid, sms=None):
    """服务端不读链：恒 None（canon_mem 因此把 id 记进 skipped）。"""
    global _mem_unsupported
    if fid:
        _mem_unsupported += 1
    return None


def mem_unsupported():
    return _mem_unsupported


_DATA_DIR = os.environ.get("SMSOCKET_DATA") or None


def sms_home():
    """服务端 runtime 根（SchemaStore/SessionStore 落盘处·可经 SMSOCKET_DATA 改）。

    无 SMSOCKET_DATA 时返回 None：由 gateway.bind_dsm() 按 db_path.parent 统一接线。
    """
    return _DATA_DIR


class _AtomicIO:
    """最小原子 JSON 读写（服务端无 SMS 的 atomic_io 模块·语义一致：临时文件 + os.replace）。"""

    @staticmethod
    def rjson(path, default=None):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    @staticmethod
    def wjson(path, doc):
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True


atomic_io = _AtomicIO()
