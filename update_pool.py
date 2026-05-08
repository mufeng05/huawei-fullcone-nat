# -*- coding: utf-8 -*-
# 主版本：仅订阅 RM / RM_ADD_DEFAULTRT 事件（PPPoE 上线秒级响应）
# 定时轮询的逻辑已拆到 update_pool_polling.py，两个脚本独立部署，
# compare-then-update 幂等，互不干扰。
# 调试输出：display logbuffer | include update_pool
import ops
import re

POOL_NAME  = "p2p_pool"
SECTION_ID = "0"
DIALER     = "Dialer1"
SECTION_RE = re.compile(r"section\s+%s\s+(\d+\.\d+\.\d+\.\d+)" % SECTION_ID,
                        re.IGNORECASE)


def _run(_ops, handle, label, cmd):
    out, nxt, desc = _ops.cli.execute(handle, cmd)
    _ops.syslog("update_pool [%s] cmd=%r out=%r next=%r desc=%r" %
                (label, cmd, out, nxt, desc))
    return out


def ops_condition(_ops):
    cond = [
        {'name': 'Ifname', 'op': 'eq', 'value': DIALER},
        {'name': 'AfType', 'op': 'eq', 'value': 'IPv4'},
    ]
    _ops.event.subscribe("rtAdd", "RM", "RM_ADD_DEFAULTRT", cond)
    _ops.correlate("rtAdd")
    return 0


def ops_execute(_ops):
    new_ip, _ = _ops.environment.get("_para_Nexthop")
    _ops.syslog("update_pool start: new_ip=%r" % new_ip)
    if not new_ip or new_ip == "0.0.0.0":
        return 0

    _ops.set_model_type("YANG")
    handle, err = _ops.cli.open()
    _ops.syslog("update_pool cli.open: handle=%r err=%r" % (handle, err))
    if handle is None:
        return 0

    try:
        _run(_ops, handle, "system-view", "system-view")
        _run(_ops, handle, "enter-pool",
             "nat address-group %s" % POOL_NAME)
        out = _run(_ops, handle, "display-this", "display this")

        m = SECTION_RE.search(out or "")
        current = m.group(1) if m else None
        _ops.syslog("update_pool compare: current=%r new_ip=%r" %
                    (current, new_ip))

        if current == new_ip:
            return 0

        _run(_ops, handle, "undo-section",
             "undo section %s" % SECTION_ID)
        _run(_ops, handle, "new-section",
             "section %s %s %s" % (SECTION_ID, new_ip, new_ip))
        _run(_ops, handle, "verify", "display this")

        _ops.syslog("update_pool done: %s %s -> %s" %
                    (DIALER, current, new_ip))
    finally:
        _ops.cli.close(handle)
    return 0
