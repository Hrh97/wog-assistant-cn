# -*- coding: utf-8 -*-
"""WoG 内核自动化工具（跨平台 / 零依赖 / 只用 Python 标准库）。

直连 ws://127.0.0.1:10998（游戏 Puerts V8 inspector）发 Runtime.evaluate，
提供两个网页定时器之外、掉页也照常工作的内核级自动化能力：

  fuse  按 fusion.GroupID 分组合成装备/饰品/宝玉（绕开游戏内置 section 对 T1 的排除）
  skill 三系技能树 SP 自动加点（复刻 companion 的 reqLevelUpAsync(4,...) recipe）
  check 只读连通性 / SP / 战场累计次数

用法：
  python wog_kernel_tools.py check
  python wog_kernel_tools.py fuse --dry          # 只报告可合组，不写游戏
  python wog_kernel_tools.py fuse --budget 8     # 真提交，最多 8 组
  python wog_kernel_tools.py skill --dry --budget 40        # 只读，展开加点计划
  python wog_kernel_tools.py skill --once --budget 6         # 真加点，渐进不超过 6 点

前置：游戏已开 10998（本仓库 wog-enable.py / 注入补丁负责），无浏览器依赖。
所有判据以游戏内核为准；--dry 全程只读。跨平台（Windows / Linux / macOS）。
"""
import base64
import json
import os
import socket
import struct
import sys

HOST, PORT = "127.0.0.1", 10998


# ---------------- 纯标准库 WebSocket 客户端 ----------------
def ws_connect(timeout=6):
    s = socket.create_connection((HOST, PORT), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((
        "GET / HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n" % (HOST, PORT, key)
    ).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = s.recv(4096)
        if not c:
            raise RuntimeError("WS handshake: connection closed")
        buf += c
    if "101" not in buf.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n", 1)[0]:
        raise RuntimeError("WS handshake failed")
    s.settimeout(30)
    return s


def _send_text(s, text):
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


def _recv_exact(s, n):
    out = b""
    while len(out) < n:
        c = s.recv(n - len(out))
        if not c:
            raise RuntimeError("connection closed")
        out += c
    return out


def _recv_text(s):
    while True:
        b1, b2 = _recv_exact(s, 2)
        op = b1 & 0x0F
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", _recv_exact(s, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", _recv_exact(s, 8))[0]
        if b2 & 0x80:
            mask = _recv_exact(s, 4)
            d = bytes(c ^ mask[i % 4] for i, c in enumerate(_recv_exact(s, n)))
        else:
            d = _recv_exact(s, n)
        if op == 1:
            return d.decode("utf-8", "replace")
        if op == 8:
            raise RuntimeError("server closed")


def evaluate(expr, mid):
    """发 Runtime.evaluate，返回已解析的 JSON（表达式统一 return JSON.stringify(out)）。"""
    s = ws_connect()
    try:
        _send_text(s, json.dumps({"id": mid, "method": "Runtime.evaluate",
                                  "params": {"expression": expr, "awaitPromise": True,
                                             "returnByValue": True}}))
        for _ in range(80):
            msg = json.loads(_recv_text(s))
            if msg.get("id") != mid:
                continue
            r = msg.get("result", {})
            if r.get("exceptionDetails"):
                raise RuntimeError("kernel exception: " + json.dumps(
                    r["exceptionDetails"], ensure_ascii=False)[:400])
            v = r.get("result", {}).get("value")
            if isinstance(v, str):
                return json.loads(v)
            return v or {}
        raise RuntimeError("no response id=%d" % mid)
    finally:
        try:
            s.close()
        except Exception:
            pass


# ---------------- 只读连通性 / SP / 战场累计 ----------------
CHECK_EXPR = r"""(function () {
  var out = { ok: true, ts: Date.now() };
  try { out.sp = {}; [8000,8100,8200].forEach(function (t) { out.sp[t] = nn.services.item.getOwnedItemCount(t); }); }
  catch (e) { out.spErr = e.message; }
  try { out.cp = Math.round(Number(nn.services.contentState._finalCp) || 0); } catch (e) {}
  try {
    var m = nn.net.data.dungeonInfo && nn.net.data.dungeonInfo._mapDungeonInfo, d = [];
    if (m && typeof m.forEach === 'function')
      m.forEach(function (v) { d.push({ tid: Number(v._dungeonTid), play: Number(v._playCnt) }); });
    out.dungeon = d;
  } catch (e) { out.dungeonErr = e.message; }
  try { out.items = nn.net.data.item.getAllItemNotStack().length; } catch (e) {}
  return JSON.stringify(out);
})()"""


# ---------------- 按 fusion.GroupID 分组合成 ----------------
# 绕开 autoRegisterFusion() 被「游戏内置自动融合 section」把 rating=1(T1) 材料排除的坑：
# 自己 collectAutoRegisterCandidates(目标不限档) -> 按 GroupID 分组 -> 满 MaterialRatingCnt 即提交。
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


# ---------------- 技能树 SP 自动加点 ----------------
# 复刻 wog-companion.js 的 syncAllocationsToLiveGame recipe，判据全以内核为准，
# 多轮贪心（加点解锁前置链后自动补后续点），材料从表内 LevelUpItemID 读、不硬编码。
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


def cmd_check(args):
    print(json.dumps(evaluate(CHECK_EXPR, 9301), ensure_ascii=False, indent=2))


def cmd_fuse(args):
    dry = "--dry" in args
    budget = _arg_int(args, "--budget", 8)
    expr = (FUSE_EXPR.replace("__DRY__", "true" if dry else "false")
                     .replace("__FUSE_BUDGET__", str(budget)))
    d = evaluate(expr, 9302)
    print("FUSE %s budget=%s before=%s after=%s fused=%s err=%s" % (
        "DRY" if dry else "COMMIT", budget, d.get("before"), d.get("after"),
        d.get("fused"), d.get("err")))
    for r in d.get("rows", []):
        print("  ct=%s r%s cand=%s ready=%s pick=%s act=%s" % (
            r.get("ct"), r.get("round"), r.get("cand"), r.get("readyGroups"),
            r.get("pick"), r.get("act")))
        if r.get("groups"):
            print("       groups: %s" % ", ".join(r["groups"]))


def cmd_skill(args):
    # 默认只读；只有显式 --once 才真写游戏，避免误操作把 SP 一次打满
    dry = "--dry" in args or "--once" not in args
    budget = _arg_int(args, "--budget", 12 if "--once" in args else 40)
    expr = (SKILL_EXPR.replace("__BUDGET__", str(budget))
                      .replace("__DRY__", "true" if dry else "false"))
    d = evaluate(expr, 9303)
    print("SKILL %s budget=%s used=%s spBefore=%s spAfter=%s error=%s" % (
        "DRY" if dry else "COMMIT", budget, d.get("used"),
        json.dumps(d.get("spBefore")), json.dumps(d.get("spAfter")), d.get("error")))
    print(json.dumps(d, ensure_ascii=False, indent=2)[:3000])


def _arg_int(args, flag, default):
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args) and args[i + 1].isdigit():
            return int(args[i + 1])
    return default


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return
    cmd, args = sys.argv[1], sys.argv[2:]
    try:
        if cmd == "check":
            cmd_check(args)
        elif cmd == "fuse":
            cmd_fuse(args)
        elif cmd == "skill":
            cmd_skill(args)
        else:
            print("unknown command: %s (use check / fuse / skill)" % cmd)
    except Exception as e:
        print("ERROR: %s: %s" % (type(e).__name__, e))
        print("（确认游戏已开 10998：本仓库 wog-enable.py --launch，或页面点「注入补丁」后重开游戏）")
        sys.exit(1)


if __name__ == "__main__":
    main()
