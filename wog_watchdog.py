# -*- coding: utf-8 -*-
"""WoG 常驻守卫（watchdog）：网页自动化掉页后自动重拉 + 内核级合成/技能点兜底。

网页里的自动化全靠浏览器 setInterval，页面一被回收（浏览器休眠、ego lite 自升版本、
标签被关）定时器就没了 —— 看起来「还在挂机」其实全停。本脚本补上这一层：

  A) 保活：每 CHECK_INTERVAL 秒检查 10998 有没有 ESTABLISHED 客户端，没有就重拉常驻页
  B) 合成：直连 10998 按 fusion.GroupID 分组合成（绕开内置 section 排除 T1 的坑）
  C) 加点：三系技能树 SP 自动加点（复刻 companion 的 reqLevelUpAsync(4,...) recipe）
  D) 版本哨兵：ego lite 自升版本后本进程的 attach 上下文会过期，检测到就提示重启

设计要点：
  - 跨平台、零第三方依赖（只用 Python 标准库）
  - 所有子进程 CREATE_NO_WINDOW + STARTUPINFO(SW_HIDE)，不会周期性弹黑框
  - 保活巡检走 ctypes GetExtendedTcpTable 纯内存枚举，不起 netstat 子进程
  - 合成/加点自适应退避：连续无产出时周期翻倍，有产出立刻回基准（实测空转 89.5%/99.2%）
  - 日志 1MB 滚动，心跳 15 分钟一条
  - 不写死本机路径：ego 位置自动发现，可用环境变量 EGO_BROWSER 覆盖

用法：
  python wog_watchdog.py              # 常驻（建议 pythonw.exe / nohup 无窗口跑）
  python wog_watchdog.py --selftest   # 三路连通性自检
  python wog_watchdog.py --fuse-once  # 只跑一次合成
  python wog_watchdog.py --skill-dry  # 只读展开加点计划

前置：游戏已开 10998（本仓库 wog_kernel_tools.py 或本地版「注入补丁」负责）；
      常驻页脚本 attach-ego-space.mjs 与它同目录。
"""

import base64
import ctypes
import json
import os
import socket
import struct
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
def _find_ego():
    """定位 ego lite 的 ego-browser 可执行文件（不写死本机路径）。

    优先环境变量 EGO_BROWSER；否则按各平台常见安装位置找 active_version_dir 符号链接：
      Windows  %LOCALAPPDATA%\\ego\\active_version_dir\\ego-browser.exe
               ~\\.local\\share\\ego\\active_version_dir\\ego-browser.exe
      macOS/Linux  ~/.local/share/ego/active_version_dir/ego-browser
    """
    env = os.environ.get("EGO_BROWSER")
    if env and os.path.exists(env):
        return env
    home = os.path.expanduser("~")
    cands = [
        os.path.join(home, ".local", "share", "ego", "active_version_dir", "ego-browser.exe"),
        os.path.join(home, ".local", "share", "ego", "active_version_dir", "ego-browser"),
        os.path.join(home, ".local", "bin", "ego-browser"),
    ]
    la = os.environ.get("LOCALAPPDATA")
    if la:
        cands.insert(0, os.path.join(la, "ego", "active_version_dir", "ego-browser.exe"))
    for c in cands:
        if os.path.exists(c):
            return c
    return cands[0]      # 找不到也返回一个候选，让报错信息指得清楚


EGO = _find_ego()
ATTACH = os.path.join(HERE, "attach-ego-space.mjs")
LOG = os.path.join(HERE, "wog-watchdog.log")

CHECK_INTERVAL = 20       # 秒：常驻页保活巡检周期
RELAUNCH_COOLDOWN = 90    # 秒：重拉后冷却，等 space 建立 + 连接稳定
RELAUNCH_TIMEOUT = 90     # 秒：单次重拉子进程超时
PORT = 10998             # 游戏 Puerts V8 inspector 端口

# —— B) 全档内核合成 ——
FUSE_INTERVAL = 90        # 秒：内核合成巡检基准周期（有产出时用这个）
FUSE_BUDGET = 10          # 单次巡检最多提交 10 组，剩下的留下一轮，避免连发压垮游戏
FUSE_WS_TIMEOUT = 25      # 秒：单次 WebSocket 读写超时
FUSE_IDLE_MAX = 300       # 秒：连续无产出时退避到的最长周期（实测 89.5% 空转，退避省内核连接）
FUSE_DRAIN = 30           # 秒：命中预算上限（有积压）时的续跑间隔，尽快排空库存

# —— C) 技能树 SP 自动加点 ——
SKILL_INTERVAL = 120      # 秒：技能树 SP 巡检基准周期（有加点时用这个）
SKILL_BUDGET = 6          # 单次巡检最多加 6 点，渐进避免瞬时打满
SKILL_WS_TIMEOUT = 30     # 秒：单次 WebSocket 读写超时
SKILL_IDLE_MAX = 1800     # 秒：连续无加点时退避到的最长周期（SP 加满后实测 99.2% 空转）

# —— 日志轮转 ——
LOG_MAX_BYTES = 1024 * 1024   # 1MB 滚动一次，避免长期挂机无限增长

_WIN = (os.name == "nt")
CREATE_NO_WINDOW = 0x08000000 if _WIN else 0

# GetExtendedTcpTable 常量
AF_INET = 2
TCP_TABLE_OWNER_PID_ALL = 5
MIB_TCP_STATE_ESTAB = 5
ERROR_INSUFFICIENT_BUFFER = 122
NO_ERROR = 0


def _rotate_log():
    """超过 LOG_MAX_BYTES 就把当前日志滚成 .1（只留一代），避免长期挂机无限增长。"""
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX_BYTES:
            old = LOG + ".1"
            if os.path.exists(old):
                os.remove(old)
            os.rename(LOG, old)
    except Exception:
        pass


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        _rotate_log()          # 先轮转再写，否则刚写的这条会被一起改名带走
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def _startupinfo():
    """隐藏子进程的窗口（配合 CREATE_NO_WINDOW 双保险）。"""
    if not _WIN:
        return None
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        return si
    except Exception:
        return None


class _TcpRow(ctypes.Structure):
    """MIB_TCPROW_OWNER_PID：6 个 DWORD，共 24 字节。"""
    _fields_ = [
        ("state", wintypes.DWORD),
        ("laddr", wintypes.DWORD),
        ("lport", wintypes.DWORD),
        ("raddr", wintypes.DWORD),
        ("rport", wintypes.DWORD),
        ("pid", wintypes.DWORD),
    ]


def _port_eq(dword):
    lo = dword & 0xFFFF
    hi = (dword >> 16) & 0xFFFF
    return PORT in (lo, hi, socket.ntohs(lo), socket.ntohs(hi))


def _has_client_ctypes():
    """纯内存枚举 TCPv4 连接表：10998 是否至少有一条 ESTABLISHED。
    成功返回 True/False；无法取表返回 None（让上层回退 netstat）。"""
    if not _WIN:
        return None
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi.dll")
        fn = iphlpapi.GetExtendedTcpTable
        fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
                       wintypes.BOOL, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        fn.restype = wintypes.DWORD

        size = wintypes.DWORD(0)
        ret = fn(None, ctypes.byref(size), False, AF_INET,
                 TCP_TABLE_OWNER_PID_ALL, 0)
        if ret != ERROR_INSUFFICIENT_BUFFER or size.value == 0:
            return None
        buf = (ctypes.c_ubyte * size.value)()
        ret = fn(ctypes.byref(buf), ctypes.byref(size), False, AF_INET,
                 TCP_TABLE_OWNER_PID_ALL, 0)
        if ret != NO_ERROR:
            return None

        n = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value
        if n < 0 or n > 100000:
            return None
        rows = (_TcpRow * n).from_address(
            ctypes.addressof(buf) + ctypes.sizeof(wintypes.DWORD))
        for i in range(n):
            r = rows[i]
            if r.state == MIB_TCP_STATE_ESTAB and (_port_eq(r.lport) or _port_eq(r.rport)):
                return True
        return False
    except Exception as e:
        log("WARN GetExtendedTcpTable failed: %s (fallback to netstat)" % e)
        return None


def _has_client_netstat():
    """回退方案：netstat（已隐藏窗口，绝不弹 cmd）。"""
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"],
            stderr=subprocess.DEVNULL, timeout=15,
            creationflags=CREATE_NO_WINDOW, startupinfo=_startupinfo(),
        ).decode("latin-1", "replace")
        for line in out.splitlines():
            if (":%d" % PORT) in line and "ESTABLISHED" in line:
                return True
        return False
    except Exception as e:
        log("WARN netstat failed: %s (treat as no-client)" % e)
        return False


def has_client():
    """10998 是否至少有一条 ESTABLISHED 客户端连接（即常驻页还在）。"""
    r = _has_client_ctypes()
    if r is not None:
        return r
    return _has_client_netstat()


# ============ B) 全档内核合成：纯标准库 WebSocket 直连 10998 ============

def _ws_connect():
    s = socket.create_connection(("127.0.0.1", PORT), timeout=6)
    s.settimeout(FUSE_WS_TIMEOUT)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((
        "GET / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n" % (PORT, key)
    ).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    if "101" not in buf.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n", 1)[0]:
        s.close()
        raise RuntimeError("WS handshake failed")
    return s


def _ws_send_text(s, text):
    p = text.encode("utf-8")
    m = os.urandom(4)
    n = len(p)
    h = bytearray([0x81])
    if n < 126:
        h.append(0x80 | n)
    elif n < 65536:
        h.append(0x80 | 126)
        h += struct.pack(">H", n)
    else:
        h.append(0x80 | 127)
        h += struct.pack(">Q", n)
    s.sendall(bytes(h) + m + bytes(b ^ m[i % 4] for i, b in enumerate(p)))


def _ws_recv_exact(s, n):
    out = b""
    while len(out) < n:
        c = s.recv(n - len(out))
        if not c:
            raise RuntimeError("ws closed")
        out += c
    return out


def _ws_recv_text(s):
    while True:
        b1, b2 = _ws_recv_exact(s, 2)
        op = b1 & 0x0F
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", _ws_recv_exact(s, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", _ws_recv_exact(s, 8))[0]
        if b2 & 0x80:
            mask = _ws_recv_exact(s, 4)
            d = bytes(c ^ mask[i % 4] for i, c in enumerate(_ws_recv_exact(s, n)))
        else:
            d = _ws_recv_exact(s, n)
        if op == 1:
            return d.decode("utf-8", "replace")
        if op == 8:
            raise RuntimeError("server closed")


# 纯内核路径：setAutoRegisterIncludeStorage(true) 含仓库，按档 autoRegisterFusion
# → isFusionStagingFull 为闸 → reqFusionStagedAsync 提交；凑不齐自动 break。
# ct1=装备(6件) ct2=饰品(3件) ct20=宝玉(6颗)，T1~T6 全档。
# reqFusionStagedAsync 撞 "already pending"（页面同时在写）时 catch 后跳过本档。
FUSE_EXPR = r"""(async function (DRY, BUDGET) {
  var out = { ts: Date.now(), fused: 0, before: 0, after: 0, budget: BUDGET, rows: [], err: null };
  function cnt() { try { return nn.net.data.item.getAllItemNotStack().length; } catch (e) { return -1; } }
  function desc(v) {
    if (v == null) return 'null';
    if (typeof v === 'object') {
      try { return (Array.isArray(v) ? 'Arr(' + v.length + ')' : 'obj') + ' keys=' + Object.keys(v).slice(0, 8).join(','); }
      catch (e) { return 'obj'; }
    }
    return '[' + typeof v + '] ' + String(v);
  }
  try {
    var ws = nn.services.workshop;
    out.before = cnt();
    var plan = [[1, '装备'], [2, '饰品'], [20, '宝玉']];
    for (var pi = 0; pi < plan.length && out.fused < BUDGET; pi++) {
      var ct = plan[pi][0], label = plan[pi][1];
      for (var round = 0; round < 8 && out.fused < BUDGET; round++) {
        // 每轮全清 + 重设，避免与页面/游戏内置自动融合抢全局 _fusionTid / _fusionSlots
        ws.clearFusionStaging();
        ws.setFusionContentType(ct);
        ws.setAutoRegisterIncludeStorage('Fusion', true);
        var cands;
        try {
          cands = ws.collectAutoRegisterCandidates('Fusion',
            function (tid, id) { return !ws.isFusionStaged(id) && ws.canStageFusionItem(tid, id); },
            true, { rating: 0, level: 0 }, 1);
        } catch (e) { out.rows.push({ ct: label, round: round, act: 'COLLECT_ERR:' + String(e && e.message || e) }); break; }

        // 按 fusion.GroupID 分组（跨 limitLevel 的同组材料可以互相凑）
        var groups = {}, order = [];
        for (var i = 0; i < cands.length; i++) {
          var c = cands[i], f = null;
          try { f = ws.resolveFusionTableByItem(c.itemTid, c.itemId); } catch (e) {}
          if (!f) continue;
          var key = String(f.GroupID);
          if (!groups[key]) { groups[key] = { fusion: f, members: [] }; order.push(key); }
          groups[key].members.push(c);
        }
        var descGroups = order.map(function (k) {
          return k + ':' + groups[k].members.length + '/' + groups[k].fusion.MaterialRatingCnt;
        });
        var readyKeys = order.filter(function (k) {
          var g = groups[k]; return g.fusion.MaterialRatingCnt > 0 && g.members.length >= g.fusion.MaterialRatingCnt;
        });
        var row = { ct: label, round: round, cand: cands.length, groups: descGroups,
                    readyGroups: readyKeys.length, pick: readyKeys.length ? readyKeys[0] : 'none' };
        if (!readyKeys.length) { row.act = 'NO_READY_GROUP'; out.rows.push(row); break; }
        if (DRY) { row.act = 'DRY'; out.rows.push(row); break; }

        // 选材料最富余的组（能连合多轮）
        var best = null;
        for (var j = 0; j < readyKeys.length; j++) {
          var g = groups[readyKeys[j]];
          if (!best || g.members.length > best.members.length) best = g;
        }
        row.pick = String(best.fusion.GroupID);
        try {
          ws.lockFusionTable(best.fusion);
          row.filled = ws.fillFusionSlots(best.fusion, cands);
          row.full = ws.isFusionStagingFull();
          row.tidBefore = String(ws._fusionTid);
          if (!row.full) { row.act = 'NOT_FULL_AFTER_FILL'; out.rows.push(row); ws.clearFusionStaging(); break; }
          var res = null;
          try { res = await ws.reqFusionStagedAsync(); }
          catch (e) { row.act = 'SUBMIT_THREW:' + String(e && e.message || e); out.rows.push(row); ws.clearFusionStaging(); break; }
          row.res = desc(res);
          row.tidAfter = String(ws._fusionTid);
          if (row.tidAfter === '0' || res != null) { row.act = 'FUSED'; out.fused++; }
          else { row.act = 'NO_CONSUME'; out.rows.push(row); ws.clearFusionStaging(); break; }
          out.rows.push(row);
          ws.clearFusionStaging();
        } catch (e) {
          row.act = 'ERR:' + String(e && e.message || e); out.rows.push(row);
          try { ws.clearFusionStaging(); } catch (e2) {}
          break;
        }
      }
    }
    try { ws.clearFusionStaging(); ws.setAutoRegisterIncludeStorage('Fusion', true); } catch (e) {}
    out.after = cnt();
  } catch (e) { out.err = e.message; }
  return JSON.stringify(out);
})(__DRY__, __FUSE_BUDGET__)"""


def kernel_fuse():
    """直连 10998 跑一次全档内核合成。返回 fused 组数；异常返回 -1。
    WebSocket 连接在本函数内开、内关，关闭后才返回 → 不会干扰 has_client() 的
    ESTABLISHED 客户端判定（那在下一次循环、sleep 之后才跑）。"""
    s = None
    try:
        s = _ws_connect()
        expr = (FUSE_EXPR.replace("__FUSE_BUDGET__", str(FUSE_BUDGET))
                 .replace("__DRY__", "false"))
        req = json.dumps({
            "id": 9701, "method": "Runtime.evaluate",
            "params": {"expression": expr, "awaitPromise": True, "returnByValue": True},
        })
        _ws_send_text(s, req)
        for _ in range(60):
            msg = json.loads(_ws_recv_text(s))
            if msg.get("id") != 9701:
                continue
            r = msg.get("result", {})
            if r.get("exceptionDetails"):
                log("FUSE EXC: " + json.dumps(r["exceptionDetails"], ensure_ascii=False)[:300])
                return -1
            v = r.get("result", {}).get("value")
            data = json.loads(v) if isinstance(v, str) else (v or {})
            if data.get("err"):
                log("FUSE kernel err: %s" % data["err"])
                return -1
            return int(data.get("fused", 0)), int(data.get("before", -1)), int(data.get("after", -1))
        return -1
    except Exception as e:
        log("FUSE skipped: %s" % e)
        return -1
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


SKILL_EXPR = r"""(async function (BUDGET, DRY) {
  var out = { ts: Date.now(), added: [], errors: [], spBefore: {}, spAfter: {}, used: 0 };
  function spSnap() {
    var it = nn.services.item, r = {};
    [8000, 8100, 8200].forEach(function (t) { try { r[t] = it.getOwnedItemCount(t); } catch (e) { r[t] = 'ERR'; } });
    return r;
  }
  try {
    var treeS = null, it = nn.services.item;
    var gdb = nn.db && nn.db.skill && nn.db.skill.group;
    if (!gdb || !gdb.getGroupTables) { out.error = 'NO_nn.db.skill.group.getGroupTables'; return JSON.stringify(out); }
    for (const [k, v] of nn.services._mapService.entries())
      if (((k && k.name) ? k.name : String(k)) === 'ServiceTree') treeS = v;
    if (!treeS || !treeS.reqLevelUpAsync) { out.error = 'NO_ServiceTree.reqLevelUpAsync'; return JSON.stringify(out); }
    out.spBefore = spSnap();

    // 三系技能树 group：81/82/83 + NN(01..35) + 010；材料从表内 LevelUpItemID 读，不硬编码
    var gids = [];
    for (var C = 1; C <= 3; C++)
      for (var NN = 1; NN <= 35; NN++)
        gids.push(Number('8' + C + (NN < 10 ? '0' : '') + NN + '010'));

    var simLv = {};      // dry 模式模拟等级推进
    var skip = {};       // 写入失败/被拒的 gid，避免死循环
    var used = 0, guard = 0;
    var GUARD_MAX = BUDGET * 120 + 400;

    // 多轮贪心：每加一点就重扫（新等级会解锁前置/提高可加档）
    while (used < BUDGET && guard++ < GUARD_MAX) {
      var didOne = false;
      for (var i = 0; i < gids.length && used < BUDGET; i++) {
        var gid = gids[i];
        if (skip[gid]) continue;
        var tables = null;
        try { tables = gdb.getGroupTables(gid); } catch (e) { continue; }
        if (!tables || !tables.length) continue;
        var realLv = 0;
        try { realLv = treeS.getSkillGroupReachedLevel(gid) || 0; } catch (e) { continue; }
        var curLv = Math.max(realLv, simLv[gid] || 0);
        if (curLv >= tables.length) continue;                 // 满级
        var nt = tables[curLv];                               // Level = curLv+1
        if (!nt) continue;
        var canOpen = true;
        try { canOpen = treeS.canOpenSkillRow ? !!treeS.canOpenSkillRow(nt) : true; } catch (e) { canOpen = false; }
        if (!canOpen) continue;                              // 前置/角色等级未满足
        var toTid = Number(nt.SkillID ?? nt.PassiveSkillID ?? 0);
        if (!toTid) continue;
        var ids = nt.LevelUpItemID || [], cnts = nt.LevelUpItemCnt || [];
        var mats = [], enough = true;
        for (var j = 0; j < Math.min(ids.length, cnts.length); j++) {
          var tid = Number(ids[j]), cnt = Number(cnts[j]);
          if (tid > 0 && cnt > 0) {
            try { if (it.hasEnoughStackItem(tid, cnt, false) === false) { enough = false; break; } } catch (e) {}
            mats.push({ itemId: 0n, itemTid: tid, itemCnt: cnt });
          }
        }
        if (!enough) continue;                              // SP 不足
        var spTid = ids[0] ? Number(ids[0]) : null;
        if (DRY) {
          out.added.push({ gid: gid, from: curLv, to: curLv + 1, toTid: toTid, spTid: spTid, dry: true });
          simLv[gid] = curLv + 1;
          used++; didOne = true;
          continue;
        }
        var ok = false;
        try { ok = await treeS.reqLevelUpAsync(4, gid, toTid, mats); }
        catch (e) { out.errors.push({ gid: gid, err: String(e && e.message || e) }); skip[gid] = 1; continue; }
        var newLv = 0;
        try { newLv = treeS.getSkillGroupReachedLevel(gid) || 0; } catch (e) {}
        if (newLv > curLv) {
          out.added.push({ gid: gid, from: curLv, to: newLv, toTid: toTid, spTid: spTid, ok: !!ok });
          simLv[gid] = newLv;
          used++; didOne = true;
        } else {
          out.errors.push({ gid: gid, reason: ok ? '等级未变化' : '游戏拒绝升级', toTid: toTid });
          skip[gid] = 1;                                    // 本轮不再试该点
        }
      }
      if (!didOne) break;
    }
    out.spAfter = spSnap();
    out.used = used;
  } catch (e) { out.error = e.message; }
  return JSON.stringify(out);
})(__BUDGET__, __DRY__)"""


def kernel_skill(dry=False):
    """直连 10998 跑一次技能树 SP 加点。返回 (used, spBefore, spAfter)；异常返回 -1。
    recipe 与 _do_skill_sp.py 完全一致（同一 SKILL_EXPR），BUDGET 用 SKILL_BUDGET，
    dry=True 时不写游戏只展开计划。WebSocket 内开内关，不干扰 has_client()。"""
    s = None
    try:
        s = _ws_connect()
        budget = 40 if dry else SKILL_BUDGET
        expr = SKILL_EXPR.replace("__BUDGET__", str(budget)).replace(
            "__DRY__", "true" if dry else "false")
        req = json.dumps({
            "id": 9702, "method": "Runtime.evaluate",
            "params": {"expression": expr, "awaitPromise": True, "returnByValue": True},
        })
        _ws_send_text(s, req)
        for _ in range(60):
            msg = json.loads(_ws_recv_text(s))
            if msg.get("id") != 9702:
                continue
            r = msg.get("result", {})
            if r.get("exceptionDetails"):
                log("SKILL EXC: " + json.dumps(r["exceptionDetails"], ensure_ascii=False)[:300])
                return -1
            v = r.get("result", {}).get("value")
            data = json.loads(v) if isinstance(v, str) else (v or {})
            if data.get("error"):
                log("SKILL kernel err: %s" % data["error"])
                return -1
            used = int(data.get("used", 0))
            before = json.dumps(data.get("spBefore", {}), ensure_ascii=False)
            after = json.dumps(data.get("spAfter", {}), ensure_ascii=False)
            if data.get("errors"):
                log("SKILL partial errors: " + json.dumps(data["errors"][:3], ensure_ascii=False)[:300])
            return used, before, after
        return -1
    except Exception as e:
        log("SKILL skipped: %s" % e)
        return -1
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


def ego_version():
    """取 ego lite 当前版本（从 active_version_dir 解析真实路径，取 Application 下版本目录名）。

    为什么要盯它：2026-11-14~16 实测，ego lite 从 0.6.2.7 自升到 0.6.3.14 后，旧 watchdog
    进程握着的 attach 上下文与新运行时不再匹配，页面被稳定每 30 分钟回收一次（89 次间隔全是 30:10）。
    版本一变就提示重启 watchdog，是这条坑的根治办法。
    """
    try:
        real = os.path.realpath(EGO)
        # ...\Application\<版本>\ego-browser.exe  ->  <版本>
        parts = [p for p in real.replace("/", "\\").split("\\") if p]
        for i, p in enumerate(parts):
            if p.lower() == "application" and i + 1 < len(parts):
                return parts[i + 1]
        return "(unknown)"
    except Exception:
        return "(unknown)"


def relaunch():
    """重跑 attach-ego-space.mjs（stdin 重定向，幂等；窗口全隐藏）。"""
    try:
        with open(ATTACH, "rb") as f:
            script = f.read()
        p = subprocess.run(
            [EGO, "nodejs"],
            input=script,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=RELAUNCH_TIMEOUT,
            cwd=HERE,
            creationflags=CREATE_NO_WINDOW,
            startupinfo=_startupinfo(),
        )
        tail = p.stdout.decode("utf-8", "replace").strip().splitlines()
        # 只留关键信号：REUSE/OPENED、bridge 状态、switchesOn、CREATED_THIS_RUN，
        # 不要把整段 dailyLog（几百字符）塞进日志行。
        keys = ("REUSE_EXISTING", "OPENED", "bridgeStatus", "switchesOn",
                "CREATED_THIS_RUN", "SPACE_ID", "PERSISTENT_PAGE_LEFT_OPEN", "ERROR")
        picked = []
        for ln in tail:
            s = ln.strip()
            if any(k in s for k in keys):
                picked.append(s.strip('| ').strip())
        summary = " | ".join(picked[:6]) if picked else " | ".join(tail[-3:])
        log("RELAUNCH exit=%s :: %s" % (p.returncode, summary[:400]))
        return p.returncode == 0
    except subprocess.TimeoutExpired:
        log("RELAUNCH TIMEOUT after %ss" % RELAUNCH_TIMEOUT)
        return False
    except Exception as e:
        log("RELAUNCH ERROR: %s" % e)
        return False


def main():
    log("=== watchdog start (pid=%s) keepalive=%ss cooldown=%ss fuse=%ss(idle<=%ss) skill=%ss(idle<=%ss) [no-window] ===" %
        (os.getpid(), CHECK_INTERVAL, RELAUNCH_COOLDOWN, FUSE_INTERVAL, FUSE_IDLE_MAX,
         SKILL_INTERVAL, SKILL_IDLE_MAX))
    log("EGO=%s" % EGO)
    log("ATTACH=%s" % ATTACH)
    ego_ver = ego_version()
    log("EGO_VERSION=%s" % ego_ver)
    last_relaunch = 0.0
    last_fuse = 0.0
    last_skill = 0.0
    last_beat = 0.0
    last_ver_check = 0.0
    # 自适应退避：连续无产出就把周期翻倍，直到 idle 上限；一旦有产出立刻回到基准周期。
    # 依据：56 小时日志实测 FUSE 空转 89.5%、SKILL 空转 99.2%，每次空转都要开一次内核 WS。
    fuse_every = FUSE_INTERVAL
    skill_every = SKILL_INTERVAL
    while True:
        # D) ego lite 版本哨兵：版本一变就说明运行时被换掉了，本进程的 attach 上下文已过期，
        #    只提示不自动重启（自杀式重启会让 watchdog 自己消失），由外部按提示重启。
        try:
            now = time.time()
            if now - last_ver_check >= 600:      # 每 10 分钟查一次，几乎零开销
                last_ver_check = now
                cur = ego_version()
                if cur != ego_ver:
                    log("EGO_VERSION CHANGED %s -> %s ; 本进程 attach 上下文已过期，"
                        "请重启 watchdog（start-watchdog.ps1），否则页面可能被周期性回收" %
                        (ego_ver, cur))
                    ego_ver = cur
        except Exception as e:
            log("VERSION SENTINEL ERROR: %s" % e)

        try:
            if has_client():
                now = time.time()
                if now - last_beat >= 900:      # 心跳每 15 分钟一条（原来 5 分钟，日志更省）
                    last_beat = now
                    log("OK 10998 has client; automation presumed alive (fuse every %ss, skill every %ss)" %
                        (fuse_every, skill_every))
            else:
                now = time.time()
                if now - last_relaunch >= RELAUNCH_COOLDOWN:
                    log("ALERT no 10998 ESTABLISHED client -> relaunching page")
                    relaunch()
                    last_relaunch = now
                else:
                    log("no client, but in cooldown (%ds left)" %
                        int(RELAUNCH_COOLDOWN - (now - last_relaunch)))
        except Exception as e:
            log("KEEPALIVE LOOP ERROR: %s" % e)

        # B) 全档内核合成（独立 try，绝不因合成异常拖垮保活）
        try:
            now = time.time()
            if now - last_fuse >= fuse_every:
                last_fuse = now
                r = kernel_fuse()
                if isinstance(r, tuple):
                    fused, before, after = r
                    if fused > 0:
                        log("FUSE ok: fused=%d groups, items %d->%d" % (fused, before, after))
                        # 命中预算上限 = 还有积压，立刻续跑把库存排空；否则回基准周期
                        fuse_every = FUSE_DRAIN if fused >= FUSE_BUDGET else FUSE_INTERVAL
                    else:
                        fuse_every = min(fuse_every * 2, FUSE_IDLE_MAX)
                    # fused=0 为正常「当前没有能凑齐一组的档」，不刷日志，只静默退避
        except Exception as e:
            log("FUSE LOOP ERROR: %s" % e)

        # C) 技能树 SP 自动加点（独立 try，绝不因加点异常拖垮保活/合成）
        try:
            now = time.time()
            if now - last_skill >= skill_every:
                last_skill = now
                r = kernel_skill()
                if isinstance(r, tuple):
                    used, before, after = r
                    if used > 0:
                        log("SKILL ok: +%d pts, SP %s->%s" % (used, before, after))
                        skill_every = SKILL_INTERVAL        # 有加点 -> 回到基准周期
                    else:
                        skill_every = min(skill_every * 2, SKILL_IDLE_MAX)
                    # used=0 为「当前无可加点 / SP 用尽 / 被前置挡」，正常，不刷日志
        except Exception as e:
            log("SKILL LOOP ERROR: %s" % e)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        print("WIN =", _WIN)
        print("ctypes  has_client =", _has_client_ctypes())
        print("netstat has_client =", _has_client_netstat())
        print("EFFECTIVE         =", has_client())
    elif "--skill-once" in sys.argv:
        print("SKILL_ONCE_RESULT:", kernel_skill())
    elif "--skill-dry" in sys.argv:
        print("SKILL_DRY_RESULT:", kernel_skill(dry=True))
    elif "--fuse-once" in sys.argv:
        r = kernel_fuse()
        print("FUSE_ONCE_RESULT:", r)
        if isinstance(r, tuple):
            print("fused=%d items %d->%d" % (r[0], r[1], r[2]))
    else:
        main()
