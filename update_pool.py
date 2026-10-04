# -*- coding: utf-8 -*-
# 主版本：仅订阅 RM / RM_ADD_DEFAULTRT 事件（PPPoE 上线秒级响应）
# 定时轮询的逻辑已拆到 update_pool_polling.py，两个脚本独立部署，
# compare-then-update 幂等，互不干扰。
# 新 IP 优先读 Dialer1 接口；读不到时退回事件参数 _para_Nexthop
# （默认路由不带下一跳时，它就是 Dialer1 自己的 IP）。
# 事件很少触发，所以每一步都记日志：more logfile/log.log | include update_pool
import ops
import re

POOL_NAME  = "p2p_pool"
SECTION_ID = "0"
DIALER     = "Dialer1"
LOG        = "update_pool"
IP_RE      = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
SECTION_RE = re.compile(r"section\s+%s\s+(\d+\.\d+\.\d+\.\d+)" % SECTION_ID,
                        re.IGNORECASE)
DIALER_RE  = re.compile(r"%s\s+(\d+\.\d+\.\d+\.\d+)" % DIALER)


def run(_ops, handle, cmd, trace):
    out, nxt, desc = _ops.cli.execute(handle, cmd)
    out = out or ""
    if trace:
        _ops.syslog("%s cmd=%r desc=%r next=%r out=%r" %
                    (LOG, cmd, desc, nxt, out.strip()[-200:]))
    return out, desc


def read_value(_ops, handle, cmd, regex, trace):
    """Run a display command and extract the first regex group; log the matched line."""
    out, desc = run(_ops, handle, cmd, False)
    m = regex.search(out)
    if trace:
        line = next((l.strip() for l in out.splitlines() if regex.search(l)), None)
        _ops.syslog("%s read %r: desc=%r line=%r" %
                    (LOG, cmd, desc, line or "no match, out=%r" % out.strip()[-300:]))
    return m.group(1) if m else None


def get_dialer_ip(_ops, handle, trace=False):
    ip = read_value(_ops, handle, "display ip interface brief %s" % DIALER, DIALER_RE, trace)
    return None if ip in (None, "0.0.0.0") else ip


def get_section_ip(_ops, handle, trace=False):
    return read_value(_ops, handle, "display nat address-group name %s" % POOL_NAME,
                      SECTION_RE, trace)


def write_section(_ops, handle, new_ip, trace):
    """Rewrite the section and read it back; the read-back decides the result.
    Returns (ok, error) where error is the first failed command, if any."""
    error = None
    for cmd in ("system-view",
                "nat address-group %s" % POOL_NAME,
                "undo section %s" % SECTION_ID,
                "section %s %s %s" % (SECTION_ID, new_ip, new_ip),
                "return"):
        _, desc = run(_ops, handle, cmd, trace)
        if desc != "Success" and not cmd.startswith("undo section"):
            error = "%s (%s)" % (cmd, desc)
            run(_ops, handle, "return", trace)
            break
    # 以回读结果为准：另一个脚本恰好同时在写时，本次的命令可能报错，但 section 已经是对的
    return get_section_ip(_ops, handle, trace) == new_ip, error


def ops_condition(_ops):
    cond = [
        {'name': 'Ifname', 'op': 'eq', 'value': DIALER},
        {'name': 'AfType', 'op': 'eq', 'value': 'IPv4'},
    ]
    _ops.event.subscribe("rtAdd", "RM", "RM_ADD_DEFAULTRT", cond)
    _ops.correlate("rtAdd")
    return 0


def ops_execute(_ops):
    hint, _ = _ops.environment.get("_para_Nexthop")
    _ops.set_model_type("YANG")
    handle, err = _ops.cli.open()
    if handle is None:
        _ops.syslog("%s cli.open failed: err=%r" % (LOG, err))
        return 0

    try:
        _ops.syslog("%s start: event nexthop=%r" % (LOG, hint))
        new_ip, source = get_dialer_ip(_ops, handle, True), DIALER
        if not new_ip and hint and IP_RE.match(hint) and hint != "0.0.0.0":
            new_ip, source = hint, "_para_Nexthop"
        elif new_ip and hint and hint != new_ip:
            _ops.syslog("%s note: event nexthop %s differs from %s address %s, "
                        "using the interface address" % (LOG, hint, DIALER, new_ip))
        current = get_section_ip(_ops, handle, True)
        _ops.syslog("%s compare: section=%r new_ip=%r (from %s)" %
                    (LOG, current, new_ip, source))
        if not new_ip or current == new_ip:
            return 0

        ok, error = write_section(_ops, handle, new_ip, True)
        if ok:
            _ops.syslog("%s done: %s %s -> %s%s" %
                        (LOG, DIALER, current, new_ip,
                         " (%s failed, but the section is already correct)" % error if error else ""))
        else:
            _ops.syslog("%s FAILED: %s %s -> %s, failed at %s" %
                        (LOG, DIALER, current, new_ip, error or "verify"))
    finally:
        _ops.cli.close(handle)
    return 0
