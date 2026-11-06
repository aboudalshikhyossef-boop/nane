"use strict";

var assert = require("assert");
var fs = require("fs");
var vm = require("vm");

function FakeText(value) {
  this.nodeType = 3;
  this.nodeValue = String(value);
  this.parentNode = null;
}

function FakeElement(tagName, id) {
  this.nodeType = 1;
  this.tagName = String(tagName || "div").toUpperCase();
  this.id = id || "";
  this.className = "";
  this.childNodes = [];
  this.parentNode = null;
  this.style = {};
  this.attributes = {};
  this.disabled = false;
  this.value = "";
  this.type = "";
  this.onclick = null;
}

Object.defineProperty(FakeElement.prototype, "firstChild", {
  get: function () {
    return this.childNodes.length ? this.childNodes[0] : null;
  }
});

FakeElement.prototype.appendChild = function (node) {
  this.childNodes.push(node);
  node.parentNode = this;
  return node;
};

FakeElement.prototype.removeChild = function (node) {
  var index = this.childNodes.indexOf(node);
  if (index >= 0) {
    this.childNodes.splice(index, 1);
    node.parentNode = null;
  }
  return node;
};

FakeElement.prototype.setAttribute = function (name, value) {
  this.attributes[name] = String(value);
};

FakeElement.prototype.getElementsByTagName = function (tag) {
  var wanted = String(tag).toUpperCase();
  var result = [];
  var i;
  function collect(node) {
    var j;
    for (j = 0; j < node.childNodes.length; j++) {
      var child = node.childNodes[j];
      if (child.nodeType === 1) {
        if (child.tagName === wanted) { result.push(child); }
        collect(child);
      }
    }
  }
  collect(this);
  return result;
};

FakeElement.prototype.select = function () {};

function elementText(node) {
  if (!node) { return ""; }
  if (node.nodeType === 3) { return node.nodeValue; }
  var result = "";
  var i;
  for (i = 0; i < node.childNodes.length; i++) {
    result += elementText(node.childNodes[i]);
  }
  return result;
}

var ids = [
  "connection", "entry", "room", "profile", "arena", "notice",
  "players", "roomCodeValue", "count", "readyButton", "readyText",
  "profileStatus", "profileButton", "startButton", "startText",
  "profileQuestions", "profileProgressText", "profileProgressPercent",
  "profileProgressBar", "profileSaveState", "arenaPhase", "arenaSubtitle",
  "scoreboardList", "arenaContent", "timerValue", "timerBar",
  "taskPhaseTag", "taskRound", "arenaStatus", "createButton",
  "joinButton", "createName", "joinName", "roomCode", "leaveButton",
  "copyButton", "profileBack", "profileDone"
];
var elements = {};
ids.forEach(function (id) { elements[id] = new FakeElement("div", id); });

var document = {
  body: new FakeElement("body", "body"),
  getElementById: function (id) {
    if (!elements[id]) { elements[id] = new FakeElement("div", id); }
    return elements[id];
  },
  createElement: function (tag) { return new FakeElement(tag); },
  createTextNode: function (value) { return new FakeText(value); },
  execCommand: function () { return true; }
};

var questionList = [];
var answers = {};
var i;
for (i = 0; i < 21; i++) {
  questionList.push({
    id: "profile_" + (i + 1),
    field_id: "field_" + (i + 1),
    text_ar: "سؤال تجريبي رقم " + (i + 1),
    category: i < 7 ? "تفضيلات" : (i < 14 ? "اهتمامات" : "اختيارات"),
    options: [
      { id: "o1", text_ar: "الخيار الأول" },
      { id: "o2", text_ar: "الخيار الثاني" }
    ],
    selected_option_id: null
  });
}

var roomState = {
  code: "NANE123456",
  all_ready: false,
  you: { id: "p1", ready: false },
  players: [
    { id: "p1", name: "مريم", seat: 1, avatar: "م", connected: true, ready: false },
    { id: "p2", name: "سامي", seat: 2, avatar: "س", connected: true, ready: false },
    { id: "p3", name: "ليلى", seat: 3, avatar: "ل", connected: true, ready: false }
  ]
};

function profilePayload() {
  return {
    ok: true,
    profile: {
      answers: JSON.parse(JSON.stringify(answers)),
      completed_count: Object.keys(answers).length,
      required_count: 21,
      is_complete: Object.keys(answers).length === 21,
      version: Object.keys(answers).length + 1,
      questions: questionList.map(function (question) {
        var copy = JSON.parse(JSON.stringify(question));
        copy.selected_option_id = answers[copy.field_id] || null;
        return copy;
      })
    }
  };
}

var matchState = null;
var lastAction = null;
var sequence = 41;

function initialMatch() {
  return {
    id: "match-test",
    status: "active",
    phase: "knowledge",
    phase_label: "من يعرفني؟",
    phase_number: 1,
    phase_count: 5,
    scoreboard: [
      { player_id: "p1", name: "مريم", seat: 1, score: 0, correct_guesses: 0 },
      { player_id: "p2", name: "سامي", seat: 2, score: 0, correct_guesses: 0 },
      { player_id: "p3", name: "ليلى", seat: 3, score: 0, correct_guesses: 0 }
    ],
    current_task: {
      kind: "knowledge",
      status: "active",
      subphase: "guess",
      round_in_phase: 1,
      rounds_in_phase: 9,
      task_sequence: sequence,
      remaining_seconds: 70,
      content: {
        question: {
          text_ar: "ما اللون المفضل؟",
          options: [
            { id: "o1", text_ar: "الأزرق" },
            { id: "o2", text_ar: "الأخضر" }
          ]
        }
      },
      my_state: { can_answer: false, can_guess: true, has_submitted: false }
    }
  };
}

function route(method, url, body) {
  if (method === "GET" && url === "/api/health") {
    return { status: 200, payload: { ok: true } };
  }
  if (method === "GET" && url === "/api/me") {
    return { status: 200, payload: { ok: true, player_id: "p1", room: roomState } };
  }
  if (method === "GET" && /\/profile$/.test(url)) {
    return { status: 200, payload: profilePayload() };
  }
  if (method === "GET" && /\/match$/.test(url)) {
    return { status: 200, payload: { ok: true, match: matchState } };
  }
  if (method === "GET" && /\/api\/rooms\//.test(url)) {
    return { status: 200, payload: { ok: true, room: roomState } };
  }
  if (method === "POST" && /\/profile$/.test(url)) {
    Object.keys(body.answers).forEach(function (key) { answers[key] = body.answers[key]; });
    return { status: 200, payload: profilePayload() };
  }
  if (method === "POST" && /\/ready$/.test(url)) {
    roomState.you.ready = !!body.ready;
    roomState.all_ready = roomState.you.ready;
    roomState.players.forEach(function (player) { player.ready = true; });
    return { status: 200, payload: { ok: true, room: roomState } };
  }
  if (method === "POST" && /\/match\/start$/.test(url)) {
    matchState = initialMatch();
    return { status: 201, payload: { ok: true, match: matchState } };
  }
  if (method === "POST" && /\/match\/action$/.test(url)) {
    lastAction = body;
    assert.strictEqual(body.task_sequence, sequence, "The client must submit the current task sequence.");
    matchState.current_task.status = "reveal";
    matchState.current_task.remaining_seconds = 12;
    matchState.current_task.result = {
      type: "knowledge",
      target_name: "ليلى",
      question: "ما اللون المفضل؟",
      answer: "الأزرق",
      score_changes: [{ player_id: "p1", player_name: "مريم", points: 2 }]
    };
    return { status: 200, payload: { ok: true, match: matchState } };
  }
  throw new Error("Unexpected request: " + method + " " + url);
}

function FakeXHR() {
  this.readyState = 0;
  this.status = 0;
  this.responseText = "";
  this.headers = {};
  this.onreadystatechange = null;
  this.onerror = null;
  this.ontimeout = null;
}
FakeXHR.prototype.open = function (method, url) {
  this.method = method;
  this.url = url;
};
FakeXHR.prototype.setRequestHeader = function (name, value) {
  this.headers[name] = value;
};
FakeXHR.prototype.send = function (rawBody) {
  var body = rawBody ? JSON.parse(rawBody) : {};
  var result = route(this.method, this.url, body);
  this.status = result.status;
  this.responseText = JSON.stringify(result.payload);
  this.readyState = 4;
  if (this.onreadystatechange) { this.onreadystatechange(); }
};

var timerId = 0;
var timers = {};
var window = {
  location: { search: "" },
  setTimeout: function (callback) {
    timerId += 1;
    timers[timerId] = callback;
    return timerId;
  },
  clearTimeout: function (id) { delete timers[id]; },
  addEventListener: function () {}
};

function fireNextTimer() {
  var keys = Object.keys(timers);
  assert.ok(keys.length, "The client should schedule its next poll.");
  var id = keys[0];
  var callback = timers[id];
  delete timers[id];
  callback();
}

function matchWithTask(kind, sequenceNo, subphase, taskContent, myState) {
  var match = initialMatch();
  match.phase_label = kind;
  match.current_task = {
    kind: kind,
    status: "active",
    subphase: subphase,
    round_in_phase: 2,
    rounds_in_phase: 6,
    task_sequence: sequenceNo,
    remaining_seconds: 40,
    content: taskContent,
    my_state: myState
  };
  return match;
}

var source = fs.readFileSync("web/js/app.js", "utf8");
vm.runInNewContext(source, {
  document: document,
  window: window,
  XMLHttpRequest: FakeXHR,
  console: console
});

assert.ok(elements.room.className.indexOf("hidden") === -1, "An existing session should open its room.");
assert.strictEqual(elementText(elements.count), "3 / 4");
assert.ok(elementText(elements.profileStatus).indexOf("0 من 21") >= 0);

elements.profileButton.onclick();
assert.ok(elements.profile.className.indexOf("hidden") === -1, "Profile screen should open.");
assert.strictEqual(elements.profileQuestions.getElementsByTagName("button").length, 42);

for (i = 0; i < 21; i++) {
  var profileButtons = elements.profileQuestions.getElementsByTagName("button");
  profileButtons[i * 2].onclick();
}

assert.strictEqual(elementText(elements.profileProgressText), "21 من 21 إجابة");
assert.ok(elementText(elements.profileSaveState).indexOf("تم حفظ") >= 0);
elements.profileDone.onclick();
assert.ok(elements.startButton.disabled, "The match must still wait for all players to be ready.");

elements.readyButton.onclick();
assert.strictEqual(elements.startButton.disabled, false, "A complete profile and ready room should enable start.");
elements.startButton.onclick();

assert.ok(elements.arena.className.indexOf("hidden") === -1, "Starting should open the match arena.");
assert.ok(elementText(elements.arenaPhase).indexOf("من يعرفني") >= 0);
assert.strictEqual(elements.arenaContent.getElementsByTagName("button").length, 2);

elements.arenaContent.getElementsByTagName("button")[0].onclick();
assert.deepStrictEqual(lastAction, {
  action: "guess",
  value: "o1",
  task_sequence: sequence
});
assert.ok(elementText(elements.arenaContent).indexOf("انكشفت الإجابة") >= 0);
assert.ok(elementText(elements.arenaContent).indexOf("الأزرق") >= 0);

/* Exercise the renderers for the remaining phases through the real polling loop. */
matchState = matchWithTask("mystery", 42, "guess", {
  title_ar: "سر صغير",
  story_ar: "هناك رسالة من أحد أفراد العائلة.",
  revealed_clues: [{ level: 1, text_ar: "يحب الأماكن الهادئة." }]
}, { can_guess: true, can_answer: false, has_submitted: false });
fireNextTimer();
assert.ok(elementText(elements.arenaContent).indexOf("سر صغير") >= 0);
assert.ok(elementText(elements.arenaContent).indexOf("يحب الأماكن الهادئة") >= 0);
assert.strictEqual(elements.arenaContent.getElementsByTagName("button").length, 3);

matchState = matchWithTask("player_question", 43, "answer", {
  question: { text_ar: "ما النشاط الذي تحبه؟", options: [{ id: "o1", text_ar: "القراءة" }, { id: "o2", text_ar: "الرياضة" }] }
}, { can_answer: true, can_guess: false, has_submitted: false });
fireNextTimer();
assert.ok(elementText(elements.arenaContent).indexOf("أنت المجيب هذه المرة") >= 0);
assert.strictEqual(elements.arenaContent.getElementsByTagName("button").length, 2);

matchState = matchWithTask("player_question", 44, "guess", {
  question: { text_ar: "ما النشاط الذي تحبه؟", options: [{ id: "o1", text_ar: "القراءة" }, { id: "o2", text_ar: "الرياضة" }] }
}, { can_answer: false, can_guess: true, has_submitted: false });
fireNextTimer();
assert.ok(elementText(elements.arenaContent).indexOf("ما الإجابة التي اختارها المجيب؟") >= 0);
assert.strictEqual(elements.arenaContent.getElementsByTagName("button").length, 2);

matchState = matchWithTask("live_choice", 45, "answer", {
  question: { text_ar: "اختر أمسيتك المثالية", options: [{ id: "o1", text_ar: "البيت" }, { id: "o2", text_ar: "الخروج" }] }
}, { can_answer: true, can_guess: false, has_submitted: false });
fireNextTimer();
assert.ok(elementText(elements.arenaContent).indexOf("اختر أمسيتك المثالية") >= 0);
assert.strictEqual(elements.arenaContent.getElementsByTagName("button").length, 2);

matchState = matchWithTask("live_choice", 46, "guess", {
  question: { text_ar: "اختر أمسيتك المثالية", options: [] }
}, { can_answer: false, can_guess: true, has_submitted: false });
matchState.current_task.anonymous_answer = "البيت";
matchState.current_task.live_guess_number = 2;
matchState.current_task.live_guess_total = 3;
fireNextTimer();
assert.ok(elementText(elements.arenaContent).indexOf("البيت") >= 0);
assert.ok(elementText(elements.arenaContent).indexOf("من صاحب الإجابة المجهولة رقم 2") >= 0);
assert.strictEqual(elements.arenaContent.getElementsByTagName("button").length, 3);

matchState = matchWithTask("finale", 47, "guess", {
  text_ar: "شخص يحب البحر ويختار المغامرة، من هو؟"
}, { can_answer: false, can_guess: true, has_submitted: false });
fireNextTimer();
assert.ok(elementText(elements.arenaContent).indexOf("شخص يحب البحر") >= 0);
assert.strictEqual(elements.arenaContent.getElementsByTagName("button").length, 3);

/* The final screen can return to the lobby without reopening the same result. */
matchState = {
  id: "match-test",
  status: "completed",
  phase: "completed",
  scoreboard: initialMatch().scoreboard,
  final_standings: [
    { player_id: "p1", name: "مريم", score: 8 },
    { player_id: "p2", name: "سامي", score: 5 },
    { player_id: "p3", name: "ليلى", score: 3 }
  ]
};
fireNextTimer();
assert.ok(elementText(elements.arenaContent).indexOf("أحسنتم يا عائلة") >= 0);
var returnButtons = elements.arenaContent.getElementsByTagName("button");
assert.strictEqual(returnButtons.length, 1);
returnButtons[0].onclick();
assert.ok(elements.room.className.indexOf("hidden") === -1);
fireNextTimer();
assert.ok(elements.arena.className.indexOf("hidden") >= 0);
assert.strictEqual(elements.startButton.disabled, false);
elements.startButton.onclick();
assert.ok(elements.arena.className.indexOf("hidden") === -1);

process.stdout.write("Browser client smoke test passed: profile, lobby, all five phase renderers, result, return and restart.\n");
