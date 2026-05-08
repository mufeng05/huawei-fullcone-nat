# -*- coding: utf-8 -*-
# 兜底：每 60 秒轮询一次 Dialer1 当前 IP，与 section 比对，不一致才更新。
# 与 update_pool.py 独立部署，compare-then-update 幂等。
# 只在真正替换 section 后才打 syslog，平时静默。
import ops
import re

POOL_NAME  = "p2p_pool"
SECTION_ID = "0"
DIALER     = "Dialer1"
SECTION_RE = re.compile(r"section\s+%s\s+(\d+\.\d+\.\d+\.\d+)" % SECTION_ID,
                        re.IGNORECASE)
DIALER_RE  = re.compile(r"%s\s+(\d+\.\d+\.\d+\.\d+)" % DIALER)


def ops_condition(_ops):
    _ops.timer.relative("tick", 60)
    _ops.correlate("tick")
    return 0


def ops_execute(_ops):
    _ops.set_model_type("YANG")
    handle, _ = _ops.cli.open()
    if handle is None:
        return 0

    try:
        out, _, _ = _ops.cli.execute(
            handle, "display ip interface brief %s" % DIALER)
        m = DIALER_RE.search(out or "")
        new_ip = m.group(1) if m else None
        if not new_ip or new_ip == "0.0.0.0":
            return 0

        _ops.cli.execute(handle, "system-view")
        _ops.cli.execute(handle, "nat address-group %s" % POOL_NAME)
        out, _, _ = _ops.cli.execute(handle, "display this")

        m = SECTION_RE.search(out or "")
        current = m.group(1) if m else None

        if current == new_ip:
            return 0

        _ops.cli.execute(handle, "undo section %s" % SECTION_ID)
        _ops.cli.execute(handle, "section %s %s %s" %
                         (SECTION_ID, new_ip, new_ip))
        _ops.syslog("update_poll done: %s %s -> %s" %
                    (DIALER, current, new_ip))
    finally:
        _ops.cli.close(handle)
    return 0
