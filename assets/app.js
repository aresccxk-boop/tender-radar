(function () {
  "use strict";

  var REGIONS = [
    { id: "henan", name: "河南" },
    { id: "guangzhou", name: "广州" },
    { id: "guangdong", name: "广东其他" },
    { id: "national", name: "全国" }
  ];
  var BUDGETS = [
    { id: "lt10", name: "10万以下" },
    { id: "10-50", name: "10–50万" },
    { id: "50-100", name: "50–100万" },
    { id: "100-300", name: "100–300万" },
    { id: "gt300", name: "300万以上" },
    { id: "unknown", name: "未公布" }
  ];
  var DUES = [
    { id: "any", name: "不限" },
    { id: "open", name: "未截止" },
    { id: "3d", name: "3天内" },
    { id: "7d", name: "7天内" },
    { id: "over", name: "已截止" },
    { id: "none", name: "无日期" }
  ];
  var PUBS = [
    { id: "any", name: "不限" },
    { id: "3d", name: "近3天" },
    { id: "7d", name: "近7天" },
    { id: "30d", name: "近30天" }
  ];
  var VIEWS = [
    { id: "week", name: "本周截止", patch: { due: "7d", ntype: ["intent", "tender", "correction"] } },
    { id: "henan-new", name: "河南新公告", patch: { region: ["henan"], pub: "7d", ntype: ["intent", "tender"] } },
    { id: "gz-ai", name: "广州 AI 实验室", patch: { region: ["guangzhou"], line: ["ai"] } },
    { id: "awards", name: "中标结果", patch: { ntype: ["award"] } }
  ];

  var rules = null;
  var raw = [];
  var meta = { updated_at: "", sources: [] };
  var enriched = [];
  var selected = {};
  var state = blankState();

  function blankState() {
    return {
      q: "", view: "", line: [], region: [], city: [], stage: [], ntype: [],
      budget: [], due: "any", pub: "any", sort: "publish", dir: "desc", page: 1, ps: 50
    };
  }

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function todayStr() {
    var parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit"
    }).format(new Date());
    return parts;
  }

  function dayNum(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || "");
    if (!m) return null;
    return Date.UTC(+m[1], +m[2] - 1, +m[3]) / 86400000;
  }

  function daysFromToday(s, today) {
    var a = dayNum(s), b = dayNum(today);
    if (a == null || b == null) return null;
    return a - b;
  }

  function hasAny(text, words) {
    if (!words) return false;
    for (var i = 0; i < words.length; i++) {
      if (words[i] && text.indexOf(words[i]) >= 0) return true;
    }
    return false;
  }

  function classify(item) {
    var title = item.title || "";
    var lines = [];
    (rules["产品线"] || []).forEach(function (line) {
      if (line.id === "other") return;
      if (hasAny(title, line["关键词"])) lines.push(line.id);
    });
    if (!lines.length) lines.push("other");

    var stage = "";
    var stageText = title + (item.buyer || "");
    (rules["学段"] || []).some(function (s) {
      if (hasAny(stageText, s["关键词"])) { stage = s.id; return true; }
      return false;
    });

    var typeText = title + " " + (item.type_hint || "");
    var ntype = "";
    (rules["公告类型"] || []).some(function (t) {
      if (hasAny(typeText, t["关键词"])) { ntype = t.id; return true; }
      return false;
    });
    if (!ntype) {
      (rules["公告类型"] || []).some(function (t) {
        if (t["名称"] === item.type_hint) { ntype = t.id; return true; }
        return false;
      });
    }
    if (!ntype) ntype = "tender";
    return { lines: lines, stage: stage, ntype: ntype };
  }

  function nameOf(list, id, field) {
    field = field || "名称";
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i][field] || list[i].name;
    return id;
  }

  function lineNames(ids) {
    return ids.map(function (id) { return nameOf(rules["产品线"] || [], id); }).join("、");
  }

  function regionLabel(item) {
    var base = nameOf(REGIONS, item.region);
    if (item.city) return base + " · " + item.city;
    if (item.region === "national" && item.province) return "全国 · " + item.province;
    return base;
  }

  function moneyText(n) {
    if (n == null || n === "") return "—";
    var num = Number(n);
    if (!isFinite(num)) return "—";
    if (Math.abs(num) >= 10000) {
      var w = num / 10000;
      var s = w >= 100 ? String(Math.round(w)) : w.toFixed(2).replace(/\.00$/, "").replace(/(\.\d)0$/, "$1");
      return s + "万";
    }
    return Math.round(num).toLocaleString("zh-CN") + "元";
  }

  function budgetId(n) {
    if (n == null || n === "") return "unknown";
    if (n < 100000) return "lt10";
    if (n < 500000) return "10-50";
    if (n < 1000000) return "50-100";
    if (n < 3000000) return "100-300";
    return "gt300";
  }

  function dueKind(deadline, today) {
    if (!deadline) return "none";
    var d = daysFromToday(deadline, today);
    if (d == null) return "none";
    if (d < 0) return "over";
    if (d <= 3) return "3d";
    if (d <= 7) return "7d";
    return "later";
  }

  function dueLabel(deadline, today) {
    var k = dueKind(deadline, today);
    if (!deadline) return { text: "—", cls: "" };
    var extra = "";
    if (k === "3d" || k === "7d") {
      var d = daysFromToday(deadline, today);
      extra = d === 0 ? " 今天" : " " + d + "天";
    }
    return { text: deadline.slice(5) + extra, cls: k === "later" ? "" : k };
  }

  function enrich() {
    var today = todayStr();
    enriched = raw.map(function (item) {
      var c = classify(item);
      return Object.assign({}, item, c, { _today: today });
    });
  }

  function stageOk(stage) {
    if (!state.stage.length) return true;
    if (!stage) return false;
    if (state.stage.indexOf(stage) >= 0) return true;
    if (stage === "middle" && (state.stage.indexOf("junior") >= 0 || state.stage.indexOf("high") >= 0)) return true;
    return false;
  }

  function regionOk(item) {
    var regions = state.region;
    var cities = state.city;
    if (!regions.length && !cities.length) return true;
    if (item.region === "henan") {
      var cityHit = !cities.length || cities.some(function (c) { return (item.city || "").indexOf(c) >= 0; });
      if (cities.length && !regions.length) return cityHit;
      if (regions.indexOf("henan") >= 0) return cityHit;
      return false;
    }
    if (!regions.length) return false;
    return regions.indexOf(item.region) >= 0;
  }

  function dueOk(item) {
    var k = dueKind(item.deadline, item._today);
    if (state.due === "any") return true;
    if (state.due === "open") return k === "3d" || k === "7d" || k === "later";
    if (state.due === "7d") return k === "3d" || k === "7d";
    if (state.due === "3d") return k === "3d";
    return k === state.due;
  }

  function pubOk(item) {
    if (state.pub === "any") return true;
    var limit = { "3d": 3, "7d": 7, "30d": 30 }[state.pub];
    if (!limit) return true;
    var age = daysFromToday(item.publish_date, item._today);
    if (age == null) return false;
    return -age <= limit && age <= 0;
  }

  function filtered() {
    var q = state.q.trim().toLowerCase();
    return enriched.filter(function (item) {
      if (state.line.length && !state.line.some(function (id) { return item.lines.indexOf(id) >= 0; })) return false;
      if (!regionOk(item)) return false;
      if (!stageOk(item.stage)) return false;
      if (state.ntype.length && state.ntype.indexOf(item.ntype) < 0) return false;
      if (state.budget.length && state.budget.indexOf(budgetId(item.budget)) < 0) return false;
      if (!dueOk(item)) return false;
      if (!pubOk(item)) return false;
      if (q) {
        var blob = [item.title, item.buyer, item.winner, item.city, item.province, (item.keywords || []).join(" ")].join(" ").toLowerCase();
        if (blob.indexOf(q) < 0) return false;
      }
      return true;
    });
  }

  function sortItems(list) {
    var dir = state.dir === "asc" ? 1 : -1;
    var key = state.sort;
    return list.slice().sort(function (a, b) {
      var av = key === "budget" ? a.budget : key === "deadline" ? a.deadline : a.publish_date;
      var bv = key === "budget" ? b.budget : key === "deadline" ? b.deadline : b.publish_date;
      var aEmpty = av == null || av === "";
      var bEmpty = bv == null || bv === "";
      if (aEmpty && bEmpty) return (a.title || "").localeCompare(b.title || "", "zh");
      if (aEmpty) return 1;
      if (bEmpty) return -1;
      if (av < bv) return -1 * dir;
      if (av > bv) return 1 * dir;
      return (a.title || "").localeCompare(b.title || "", "zh");
    });
  }

  function readUrl() {
    var p = new URLSearchParams(location.search);
    state = blankState();
    state.q = p.get("q") || "";
    state.view = p.get("view") || "";
    state.line = split(p.get("line"));
    state.region = split(p.get("region"));
    state.city = split(p.get("city"));
    state.stage = split(p.get("stage"));
    state.ntype = split(p.get("ntype"));
    state.budget = split(p.get("budget"));
    state.due = p.get("due") || "any";
    state.pub = p.get("pub") || "any";
    state.sort = p.get("sort") || "publish";
    state.dir = p.get("dir") === "asc" ? "asc" : "desc";
    state.page = Math.max(1, parseInt(p.get("page") || "1", 10) || 1);
    var ps = parseInt(p.get("ps") || "50", 10);
    state.ps = ps === 30 || ps === 100 ? ps : 50;
    if (state.view && !p.get("line") && !p.get("region") && !p.get("ntype") && !p.get("due") && !p.get("pub")) {
      applyViewPatch(state.view, false);
    }
  }

  function split(v) { return v ? v.split(",").filter(Boolean) : []; }

  function writeUrl() {
    var p = new URLSearchParams();
    if (state.q) p.set("q", state.q);
    if (state.view) p.set("view", state.view);
    ["line", "region", "city", "stage", "ntype", "budget"].forEach(function (k) {
      if (state[k].length) p.set(k, state[k].join(","));
    });
    if (state.due !== "any") p.set("due", state.due);
    if (state.pub !== "any") p.set("pub", state.pub);
    if (state.sort !== "publish") p.set("sort", state.sort);
    if (state.dir !== "desc") p.set("dir", state.dir);
    if (state.page > 1) p.set("page", String(state.page));
    if (state.ps !== 50) p.set("ps", String(state.ps));
    var qs = p.toString();
    history.replaceState(null, "", qs ? "?" + qs : location.pathname);
  }

  function applyViewPatch(id) {
    var view = VIEWS.filter(function (v) { return v.id === id; })[0];
    if (!view) return;
    var keepQ = state.q;
    var ps = state.ps;
    state = blankState();
    state.q = keepQ;
    state.ps = ps;
    state.view = id;
    var patch = view.patch;
    Object.keys(patch).forEach(function (k) { state[k] = Array.isArray(patch[k]) ? patch[k].slice() : patch[k]; });
  }

  function dropView() { state.view = ""; }

  function renderChecks(el, options, selectedIds, multi) {
    el.innerHTML = options.map(function (opt) {
      var on = multi ? selectedIds.indexOf(opt.id) >= 0 : state[el.dataset.key] === opt.id;
      return '<label class="' + (on ? "on" : "") + '"><input type="' + (multi ? "checkbox" : "radio") + '" name="' + el.id + '" value="' + esc(opt.id) + '"' + (on ? " checked" : "") + ">" + esc(opt.name) + "</label>";
    }).join("");
  }

  function bindBox(id, key, options, multi) {
    var el = $(id);
    el.dataset.key = key;
    el.onchange = function (e) {
      var input = e.target;
      if (!input || input.tagName !== "INPUT") return;
      var value = input.value;
      if (multi) {
        var has = state[key].indexOf(value) >= 0;
        if (input.checked && !has) state[key].push(value);
        if (!input.checked && has) state[key] = state[key].filter(function (x) { return x !== value; });
      } else {
        state[key] = value;
      }
      dropView();
      state.page = 1;
      paint();
    };
    el._options = options;
    el._multi = multi;
  }

  function paintFilters() {
    renderChecks($("f-line"), rules["产品线"] || [], state.line, true);
    renderChecks($("f-region"), REGIONS, state.region, true);
    renderChecks($("f-city"), (rules["河南地市"] || []).map(function (c) { return { id: c, name: c }; }), state.city, true);
    var stages = (rules["学段"] || []).slice();
    renderChecks($("f-stage"), stages, state.stage, true);
    renderChecks($("f-ntype"), rules["公告类型"] || [], state.ntype, true);
    renderChecks($("f-budget"), BUDGETS, state.budget, true);
    renderChecks($("f-due"), DUES, [], false);
    renderChecks($("f-pub"), PUBS, [], false);
    $("views").innerHTML = VIEWS.map(function (v) {
      return '<button type="button" data-view="' + v.id + '" class="' + (state.view === v.id ? "on" : "") + '">' + esc(v.name) + "</button>";
    }).join("");
    $("q").value = state.q;
    $("ps").value = String(state.ps);
  }

  function arrow(key) {
    if (state.sort !== key) return "";
    return '<span class="arr">' + (state.dir === "asc" ? "↑" : "↓") + "</span>";
  }

  function renderTable(list) {
    document.querySelector('[data-sort="budget"]').innerHTML = "预算" + arrow("budget");
    document.querySelector('[data-sort="publish"]').innerHTML = "发布" + arrow("publish");
    document.querySelector('[data-sort="deadline"]').innerHTML = "截止" + arrow("deadline");
    var pages = Math.max(1, Math.ceil(list.length / state.ps));
    if (state.page > pages) state.page = pages;
    var start = (state.page - 1) * state.ps;
    var pageRows = list.slice(start, start + state.ps);
    var body = $("rows");
    if (!pageRows.length) {
      body.innerHTML = '<tr><td class="empty" colspan="11">没有符合条件的公告。换个筛选，或等下一次数据更新。</td></tr>';
    } else {
      body.innerHTML = pageRows.map(function (item) {
        var due = dueLabel(item.deadline, item._today);
        var ntypeName = nameOf(rules["公告类型"] || [], item.ntype);
        var stageName = item.stage ? nameOf(rules["学段"] || [], item.stage) : "—";
        var winner = item.winner ? '<div class="sub">成交：' + esc(item.winner) + "</div>" : "";
        var checked = selected[item.id] ? " checked" : "";
        return '<tr>' +
          '<td class="ck"><input type="checkbox" data-id="' + esc(item.id) + '"' + checked + ' aria-label="勾选"></td>' +
          '<td class="title"><a href="' + esc(item.url) + '" target="_blank" rel="noopener">' + esc(item.title) + "</a>" + winner + "</td>" +
          "<td>" + esc(regionLabel(item)) + "</td>" +
          "<td>" + item.lines.map(function (id) { return '<span class="tag">' + esc(nameOf(rules["产品线"] || [], id)) + "</span>"; }).join("") + "</td>" +
          "<td>" + esc(stageName) + "</td>" +
          '<td><span class="tag' + (item.ntype === "award" ? " award" : "") + '">' + esc(ntypeName) + "</span></td>" +
          "<td>" + esc(item.buyer || "—") + "</td>" +
          '<td class="num">' + esc(moneyText(item.budget)) + "</td>" +
          '<td class="date">' + esc(item.publish_date || "—") + "</td>" +
          '<td class="date due ' + due.cls + '">' + esc(due.text) + "</td>" +
          "<td>" + esc(item.source || "") + "</td>" +
          "</tr>";
      }).join("");
    }
    var allOn = pageRows.length > 0 && pageRows.every(function (item) { return selected[item.id]; });
    $("check-page").checked = allOn;
    $("page-label").textContent = state.page + " / " + pages;
    $("prev").disabled = state.page <= 1;
    $("next").disabled = state.page >= pages;
    var sum = 0, known = 0, picked = 0;
    list.forEach(function (item) {
      if (item.budget != null && item.budget !== "") { sum += Number(item.budget); known++; }
      if (selected[item.id]) picked++;
    });
    $("count").textContent = String(list.length);
    $("sum").textContent = known ? "预算合计 " + moneyText(sum) + "（" + known + " 条有金额）" : "";
    $("picked").textContent = picked ? "已选 " + picked + " 条" : "";
    $("copy-text").textContent = picked ? "复制摘要（" + picked + "）" : "复制摘要";
  }

  function renderSources() {
    var el = $("sources");
    el.innerHTML = (meta.sources || []).map(function (s) {
      var title = (s.note || "") + (s.kept != null ? "，入库 " + s.kept + " 条" : "");
      return '<span class="pill ' + esc(s.status || "") + '" title="' + esc(title) + '"><i></i>' + esc(s.name || s.id) + "</span>";
    }).join("");
    $("updated").textContent = meta.updated_at ? "数据更新于 " + meta.updated_at + "（北京时间）" : "暂无更新时间";
  }

  var current = [];
  function paint() {
    paintFilters();
    current = sortItems(filtered());
    renderTable(current);
    writeUrl();
  }

  function toast(msg) {
    var el = $("toast");
    el.textContent = msg;
    el.className = "show";
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { el.className = ""; }, 2200);
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(function () { toast("已复制"); }, function () { fallbackCopy(text); });
    }
    fallbackCopy(text);
  }
  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); toast("已复制"); }
    catch (e) { toast("复制失败，请手动选择"); }
    ta.remove();
  }

  function summaryText(items) {
    var lines = ["【共时招标雷达】" + items.length + " 条", ""];
    items.forEach(function (item, i) {
      lines.push((i + 1) + ". " + (item.title || ""));
      lines.push("采购单位：" + (item.buyer || "未公布"));
      lines.push("预算：" + (item.budget == null || item.budget === "" ? "未公布" : moneyText(item.budget)));
      if (item.winner) lines.push("成交供应商：" + item.winner);
      lines.push("截止：" + (item.deadline || "未公布"));
      lines.push("链接：" + item.url);
      lines.push("");
    });
    return lines.join("\n").replace(/\n+$/, "");
  }

  function selectedItems() {
    var ids = selected;
    return current.filter(function (item) { return ids[item.id]; });
  }

  function download(filename, content, mime) {
    var blob = new Blob([content], { type: mime });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }

  function rowsForExport(items) {
    return items.map(function (item) {
      return [
        item.title || "",
        item.buyer || "",
        regionLabel(item),
        item.city || "",
        lineNames(item.lines),
        item.stage ? nameOf(rules["学段"] || [], item.stage) : "",
        nameOf(rules["公告类型"] || [], item.ntype),
        item.budget == null || item.budget === "" ? "" : String(item.budget),
        item.budget == null || item.budget === "" ? "" : moneyText(item.budget),
        item.publish_date || "",
        item.deadline || "",
        item.winner || "",
        item.source || "",
        item.url || ""
      ];
    });
  }

  var EXPORT_HEAD = ["标题", "采购单位", "地区", "地市", "产品线", "学段", "公告类型", "预算元", "预算", "发布日期", "截止日期", "成交供应商", "来源", "链接"];

  function xml(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function toXls(items) {
    var head = "<Row>" + EXPORT_HEAD.map(function (h) { return '<Cell><Data ss:Type="String">' + xml(h) + "</Data></Cell>"; }).join("") + "</Row>";
    var body = rowsForExport(items).map(function (row) {
      return "<Row>" + row.map(function (cell, idx) {
        var type = idx === 7 && cell !== "" ? "Number" : "String";
        return '<Cell><Data ss:Type="' + type + '">' + xml(cell) + "</Data></Cell>";
      }).join("") + "</Row>";
    }).join("");
    return '<?xml version="1.0" encoding="UTF-8"?>' +
      '<?mso-application progid="Excel.Sheet"?>' +
      '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">' +
      '<Worksheet ss:Name="招标"><Table>' + head + body + "</Table></Worksheet></Workbook>";
  }

  function toCsv(items) {
    function cell(v) {
      var s = String(v == null ? "" : v);
      if (/[",\n]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
      return s;
    }
    var lines = [EXPORT_HEAD.map(cell).join(",")];
    rowsForExport(items).forEach(function (row) { lines.push(row.map(cell).join(",")); });
    return "\ufeff" + lines.join("\r\n");
  }

  function filterDescription() {
    var bits = [];
    if (state.q) bits.push("关键词「" + state.q + "」");
    if (state.view) {
      var view = VIEWS.filter(function (v) { return v.id === state.view; })[0];
      if (view) bits.push(view.name);
    }
    if (state.line.length) bits.push(state.line.map(function (id) { return nameOf(rules["产品线"] || [], id); }).join("、"));
    if (state.region.length) bits.push(state.region.map(function (id) { return nameOf(REGIONS, id); }).join("、"));
    if (state.city.length) bits.push(state.city.join("、"));
    if (state.ntype.length) bits.push(state.ntype.map(function (id) { return nameOf(rules["公告类型"] || [], id); }).join("、"));
    if (state.due !== "any") bits.push("截止" + nameOf(DUES, state.due, "name"));
    return bits.join(" / ") || "全部公告";
  }

  function bind() {
    bindBox("f-line", "line", [], true);
    bindBox("f-region", "region", [], true);
    bindBox("f-city", "city", [], true);
    bindBox("f-stage", "stage", [], true);
    bindBox("f-ntype", "ntype", [], true);
    bindBox("f-budget", "budget", [], true);
    bindBox("f-due", "due", [], false);
    bindBox("f-pub", "pub", [], false);

    $("views").onclick = function (e) {
      var btn = e.target.closest ? e.target.closest("button") : null;
      if (!btn) return;
      applyViewPatch(btn.getAttribute("data-view"));
      state.page = 1;
      paint();
    };
    var qTimer;
    $("q").addEventListener("input", function () {
      var value = this.value;
      clearTimeout(qTimer);
      qTimer = setTimeout(function () {
        state.q = value;
        dropView();
        state.page = 1;
        paint();
      }, 250);
    });
    $("clear").onclick = function () {
      var ps = state.ps;
      state = blankState();
      state.ps = ps;
      selected = {};
      paint();
    };
    document.querySelectorAll(".sortable").forEach(function (th) {
      th.onclick = function () {
        var key = th.getAttribute("data-sort");
        if (state.sort === key) state.dir = state.dir === "asc" ? "desc" : "asc";
        else { state.sort = key; state.dir = key === "deadline" ? "asc" : "desc"; }
        state.page = 1;
        paint();
      };
    });
    $("rows").onclick = function (e) {
      var input = e.target;
      if (input && input.getAttribute && input.getAttribute("data-id")) {
        var id = input.getAttribute("data-id");
        if (input.checked) selected[id] = true;
        else delete selected[id];
        renderTable(current);
      }
    };
    $("check-page").onclick = function () {
      var pages = Math.max(1, Math.ceil(current.length / state.ps));
      var start = (Math.min(state.page, pages) - 1) * state.ps;
      current.slice(start, start + state.ps).forEach(function (item) {
        if ($("check-page").checked) selected[item.id] = true;
        else delete selected[item.id];
      });
      renderTable(current);
    };
    $("select-all").onclick = function () {
      current.forEach(function (item) { selected[item.id] = true; });
      renderTable(current);
    };
    $("select-none").onclick = function () {
      selected = {};
      renderTable(current);
    };
    $("prev").onclick = function () { if (state.page > 1) { state.page--; paint(); } };
    $("next").onclick = function () { state.page++; paint(); };
    $("ps").onchange = function () { state.ps = parseInt(this.value, 10); state.page = 1; paint(); };
    $("copy-link").onclick = function () { writeUrl(); copyText(location.href); };
    $("copy-text").onclick = function () {
      var items = selectedItems();
      if (!items.length) { toast("请先勾选公告"); return; }
      copyText(summaryText(items));
    };
    $("export-xls").onclick = function () {
      download("共时招标雷达.xls", toXls(current), "application/vnd.ms-excel");
    };
    $("export-csv").onclick = function () {
      download("共时招标雷达.csv", toCsv(current), "text/csv;charset=utf-8");
    };
    $("print").onclick = function () { window.print(); };
    $("open-filter").onclick = function () { $("side").classList.add("open"); $("mask").classList.add("show"); };
    $("close-filter").onclick = closeSide;
    $("mask").onclick = closeSide;

    window.addEventListener("beforeprint", function () {
      var old = document.getElementById("print-head");
      if (old) old.remove();
      var head = document.createElement("div");
      head.id = "print-head";
      head.className = "print-head";
      head.innerHTML = "<h1>共时招标雷达</h1><p>" + esc(filterDescription()) + " · " + current.length + " 条 · 数据更新于 " + esc(meta.updated_at || "") + "</p><p>深圳共时科技 · 数字员工团队出品</p>";
      document.querySelector("main").prepend(head);
      var savedPage = state.page, savedPs = state.ps;
      state.page = 1;
      state.ps = Math.max(current.length, 1);
      renderTable(current);
      window.addEventListener("afterprint", function restore() {
        window.removeEventListener("afterprint", restore);
        state.page = savedPage;
        state.ps = savedPs;
        var node = document.getElementById("print-head");
        if (node) node.remove();
        renderTable(current);
      });
    });
  }

  function closeSide() {
    $("side").classList.remove("open");
    $("mask").classList.remove("show");
  }

  function boot() {
    readUrl();
    bind();
    Promise.all([
      fetch("data/tenders.json", { cache: "no-cache" }).then(function (r) { if (!r.ok) throw new Error("数据文件"); return r.json(); }),
      fetch("config/rules.json", { cache: "no-cache" }).then(function (r) { if (!r.ok) throw new Error("规则文件"); return r.json(); })
    ]).then(function (pair) {
      meta = pair[0] || {};
      raw = meta.items || [];
      rules = pair[1];
      enrich();
      renderSources();
      paint();
    }).catch(function (err) {
      $("updated").textContent = "数据读取失败：" + err.message;
      $("rows").innerHTML = '<tr><td class="empty" colspan="11">没有读到 data/tenders.json 或 config/rules.json。</td></tr>';
    });
  }

  boot();
})();
