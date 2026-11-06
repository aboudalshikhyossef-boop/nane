(function () {
"use strict";

var room = null;
var playerId = null;
var profileData = null;
var currentMatch = null;
var currentView = "entry";
var dismissedMatchId = null;
var busy = false;
var polling = false;
var pollTimer = null;
var taskKey = "";
var profileSaving = false;
var pendingProfileAnswers = {};

function byId(value) { return document.getElementById(value); }

function clear(node) {
  while (node && node.firstChild) { node.removeChild(node.firstChild); }
}

function make(tag, className, value) {
  var node = document.createElement(tag);
  if (className) { node.className = className; }
  if (value !== undefined && value !== null) {
    node.appendChild(document.createTextNode(String(value)));
  }
  return node;
}

function setText(node, value) {
  clear(node);
  if (node) {
    node.appendChild(document.createTextNode(value === null || value === undefined ? "" : String(value)));
  }
}

function message(value, good) {
  var node = byId("notice");
  setText(node, value);
  node.className = "notice" + (good ? " good" : "");
}

function hideMessage() {
  byId("notice").className = "notice hidden";
}

function online(value) {
  byId("connection").className = "connection " + (value ? "online" : "offline");
  setText(byId("connection"), value ? "متصل بالخادم" : "الاتصال غير مستقر");
}

function api(method, path, data, done) {
  var request = new XMLHttpRequest();
  var finished = false;
  request.open(method, path, true);
  request.timeout = 10000;
  if (method === "POST") {
    request.setRequestHeader("Content-Type", "application/json; charset=UTF-8");
  }
  function finish(payload, status) {
    if (finished) { return; }
    finished = true;
    done(payload, status);
  }
  request.onreadystatechange = function () {
    if (request.readyState === 4) {
      var payload = {};
      try { payload = JSON.parse(request.responseText || "{}"); } catch (ignore) {}
      finish(payload, request.status);
    }
  };
  request.onerror = function () { finish({ ok: false, error: "تعذر الاتصال بالخادم." }, 0); };
  request.ontimeout = function () { finish({ ok: false, error: "انتهت مهلة الاتصال." }, 0); };
  request.send(data ? JSON.stringify(data) : null);
}

function roomQuery() {
  var parts = (window.location.search || "").replace(/^\?/, "").split("&");
  var i;
  for (i = 0; i < parts.length; i++) {
    var pair = parts[i].split("=");
    if (decodeURIComponent(pair[0] || "") === "room") {
      try { return decodeURIComponent((pair[1] || "").replace(/\+/g, " ")).toUpperCase(); }
      catch (ignore) { return ""; }
    }
  }
  return "";
}

function stopPolling() {
  if (pollTimer) { window.clearTimeout(pollTimer); pollTimer = null; }
}

function showView(name) {
  currentView = name;
  byId("entry").className = name === "entry" ? "" : "hidden";
  byId("room").className = name === "room" ? "panel room" : "panel room hidden";
  byId("profile").className = name === "profile" ? "panel profile-screen" : "panel profile-screen hidden";
  byId("arena").className = name === "arena" ? "" : "hidden";
}

function schedule() {
  stopPolling();
  if (room) {
    pollTimer = window.setTimeout(refresh, currentView === "arena" ? 1450 : 1800);
  }
}

function renderRoom(state) {
  room = state;
  var players = state.players || [];
  var list = byId("players");
  var i;
  clear(list);
  for (i = 0; i < players.length; i++) {
    var player = players[i];
    var row = make("div", "player");
    var avatar = make("span", "avatar", player.avatar || player.seat);
    var copy = make("div", "player-copy");
    var name = make("strong", "", player.name + (player.id === playerId ? " (أنت)" : ""));
    var seat = make("small", "", "المقعد " + player.seat);
    var status = make("span", "player-state", player.connected ? (player.ready ? "مستعد" : "متصل") : "غير متصل");
    copy.appendChild(name);
    copy.appendChild(seat);
    row.appendChild(avatar);
    row.appendChild(copy);
    row.appendChild(status);
    list.appendChild(row);
  }
  setText(byId("roomCodeValue"), state.code || "");
  setText(byId("count"), players.length + " / 4");
  var you = state.you || null;
  byId("readyButton").disabled = !you || busy;
  setText(byId("readyButton"), you && you.ready ? "إلغاء الاستعداد" : "أنا مستعد");
  setText(byId("readyText"), state.all_ready ?
    "الجميع متصل ومستعد. تحقّقوا من اكتمال الملفات الشخصية لبدء اللعب." :
    "تحتاجون إلى 3 لاعبين على الأقل، ويجب أن يكون الجميع متصلين ومستعدين.");
  updateProfileSummary();
}

function updateProfileSummary() {
  var count = profileData ? profileData.completed_count : 0;
  var total = profileData ? profileData.required_count : 21;
  var complete = profileData ? profileData.is_complete : false;
  setText(byId("profileStatus"), profileData ? (complete ? "ملفك مكتمل — أحسنت!" : count + " من " + total + " إجابة مكتملة") : "جارٍ تحميل الأسئلة...");
  setText(byId("profileButton"), complete ? "مراجعة الملف الشخصي" : "إكمال الملف الشخصي");
  var canStart = !!(room && room.all_ready && complete);
  byId("startButton").disabled = !canStart || busy;
  if (!room || !room.all_ready) {
    setText(byId("startText"), "ستبدأ المباراة عندما يكون 3 أو 4 لاعبين متصلين ومستعدين، وتكون الملفات الشخصية مكتملة.");
  } else if (!complete) {
    setText(byId("startText"), "أكمل ملفك الشخصي أولًا. يجب أن يجيب جميع اللاعبين عن الأسئلة الـ21.");
  } else {
    setText(byId("startText"), "أنت جاهز. سيؤكد الخادم اكتمال ملفات بقية اللاعبين قبل بدء المباراة.");
  }
}

function loadProfile(done) {
  if (!room) { return; }
  api("GET", "/api/rooms/" + encodeURIComponent(room.code) + "/profile", null, function (payload, status) {
    if (status === 200 && payload.ok && payload.profile) {
      profileData = payload.profile;
      updateProfileSummary();
      if (currentView === "profile") { renderProfile(); }
      if (done) { done(true); }
    } else {
      if (done) { done(false); }
      if (status !== 0) { message(payload.error || "تعذر تحميل الملف الشخصي.", false); }
    }
  });
}

function enter(payload) {
  room = payload.room || payload;
  playerId = payload.player_id || (room.you ? room.you.id : null);
  showView("room");
  renderRoom(room);
  profileData = null;
  loadProfile();
  checkMatch();
  schedule();
}

function checkMatch() {
  if (!room || polling) { return; }
  api("GET", "/api/rooms/" + encodeURIComponent(room.code) + "/match", null, function (payload, status) {
    if (status === 200 && payload.ok && payload.match) {
      currentMatch = payload.match;
      if (currentMatch.status === "active" || currentMatch.status === "completed") {
        showView("arena");
        renderArena(currentMatch);
      }
    } else if (status === 401) {
      showEntry();
      message("انتهت الجلسة. أدخل رمز الغرفة للانضمام مجددًا.", false);
    }
  });
}

function showEntry() {
  stopPolling();
  room = null;
  playerId = null;
  currentMatch = null;
  profileData = null;
  taskKey = "";
  showView("entry");
}

function refresh() {
  if (!room || polling) { schedule(); return; }
  polling = true;
  if (currentView === "arena") {
    api("GET", "/api/rooms/" + encodeURIComponent(room.code) + "/match", null, function (payload, status) {
      polling = false;
      if (status === 200 && payload.ok && payload.match) {
        online(true);
        currentMatch = payload.match;
        renderArena(currentMatch);
      } else if (status === 401) {
        showEntry();
        message("انتهت الجلسة. أدخل رمز الغرفة للانضمام مجددًا.", false);
      } else {
        online(false);
        setText(byId("arenaStatus"), "تعذر تحديث المباراة؛ سنحاول الاتصال مجددًا.");
      }
      if (room) { schedule(); }
    });
    return;
  }
  api("GET", "/api/rooms/" + encodeURIComponent(room.code), null, function (payload, status) {
    if (status === 200 && payload.ok && payload.room) {
      online(true);
      renderRoom(payload.room);
      api("GET", "/api/rooms/" + encodeURIComponent(room.code) + "/match", null, function (matchPayload, matchStatus) {
        polling = false;
        if (matchStatus === 200 && matchPayload.ok && matchPayload.match &&
            (matchPayload.match.status === "active" || (matchPayload.match.status === "completed" && matchPayload.match.id !== dismissedMatchId))) {
          currentMatch = matchPayload.match;
          taskKey = "";
          showView("arena");
          renderArena(currentMatch);
        } else if (matchStatus === 401) {
          showEntry();
          message("انتهت الجلسة. أدخل رمز الغرفة للانضمام مجددًا.", false);
        }
        if (room) { schedule(); }
      });
    } else {
      polling = false;
      online(false);
      if (status === 401) {
        showEntry();
        message("انتهت الجلسة. أدخل رمز الغرفة للانضمام مجددًا.", false);
      } else {
        message(payload.error || "تعذر تحديث الغرفة.", false);
      }
      if (room) { schedule(); }
    }
  });
}

function busyState(value) {
  busy = value;
  byId("createButton").disabled = value;
  byId("joinButton").disabled = value;
  updateProfileSummary();
}

function createRoom() {
  if (busy) { return; }
  hideMessage();
  busyState(true);
  api("POST", "/api/rooms", { name: byId("createName").value }, function (payload, status) {
    busyState(false);
    if (status === 201 && payload.ok) { enter(payload); }
    else { message(payload.error || "تعذر إنشاء الغرفة.", false); }
  });
}

function joinRoom() {
  if (busy) { return; }
  hideMessage();
  var code = byId("roomCode").value.replace(/[^A-Za-z0-9]/g, "").toUpperCase();
  if (!code) { message("اكتب رمز الغرفة أولًا.", false); return; }
  busyState(true);
  api("POST", "/api/rooms/" + encodeURIComponent(code) + "/join", { name: byId("joinName").value }, function (payload, status) {
    busyState(false);
    if (status === 201 && payload.ok) { enter(payload); }
    else { message(payload.error || "تعذر الانضمام.", false); }
  });
}

function ready() {
  if (!room || !room.you || busy) { return; }
  busy = true;
  byId("readyButton").disabled = true;
  api("POST", "/api/rooms/" + encodeURIComponent(room.code) + "/ready", { ready: !room.you.ready }, function (payload, status) {
    busy = false;
    if (status === 200 && payload.ok) { renderRoom(payload.room); }
    else { byId("readyButton").disabled = false; message(payload.error || "تعذر تحديث الاستعداد.", false); }
    schedule();
  });
}

function startMatch() {
  if (!room || busy || byId("startButton").disabled) { return; }
  busy = true;
  byId("startButton").disabled = true;
  setText(byId("startText"), "جارٍ تجهيز المباراة...");
  api("POST", "/api/rooms/" + encodeURIComponent(room.code) + "/match/start", {}, function (payload, status) {
    busy = false;
    if ((status === 201 || status === 200) && payload.ok && payload.match) {
      currentMatch = payload.match;
      dismissedMatchId = null;
      taskKey = "";
      showView("arena");
      renderArena(currentMatch);
      schedule();
    } else {
      message(payload.error || "لم نتمكن من بدء المباراة. تأكدوا من اكتمال ملفات الجميع.", false);
      updateProfileSummary();
    }
  });
}

function leave() {
  if (!room || busy) { return; }
  busy = true;
  api("POST", "/api/rooms/" + encodeURIComponent(room.code) + "/leave", {}, function (payload, status) {
    busy = false;
    if (status === 200 && payload.ok) { showEntry(); hideMessage(); }
    else { message(payload.error || "تعذرت مغادرة الغرفة.", false); }
  });
}

function copyCode() {
  if (!room || !room.code) { return; }
  var code = room.code;
  if (window.clipboardData && window.clipboardData.setData) {
    window.clipboardData.setData("Text", code);
    message("تم نسخ رمز الغرفة.", true);
    return;
  }
  var box = document.createElement("textarea");
  box.value = code;
  box.style.position = "absolute";
  box.style.right = "-9999px";
  document.body.appendChild(box);
  box.select();
  try {
    var copied = document.execCommand("copy");
    message(copied ? "تم نسخ رمز الغرفة." : "رمز الغرفة: " + code, !!copied);
  } catch (ignore) { message("رمز الغرفة: " + code, false); }
  document.body.removeChild(box);
}

/* Personal profile */
function showProfile() {
  if (!room) { return; }
  if (profileData && profileData.questions) {
    showView("profile");
    renderProfile();
  } else {
    loadProfile(function (success) {
      if (success) { showView("profile"); renderProfile(); }
    });
  }
}

function renderProfile() {
  if (!profileData) { return; }
  var root = byId("profileQuestions");
  var questions = profileData.questions || [];
  var currentCategory = "";
  var categoryNode = null;
  var completed = profileData.completed_count || 0;
  var i;
  clear(root);
  for (i = 0; i < questions.length; i++) {
    var question = questions[i];
    if (question.category !== currentCategory) {
      currentCategory = question.category;
      categoryNode = make("section", "question-group");
      categoryNode.appendChild(make("h3", "group-title", currentCategory));
      root.appendChild(categoryNode);
    }
    var card = make("article", "question-card");
    var title = make("div", "question-title");
    title.appendChild(make("span", "question-number", i + 1));
    title.appendChild(make("strong", "", question.text_ar));
    card.appendChild(title);
    var options = make("div", "question-options");
    var j;
    for (j = 0; j < question.options.length; j++) {
      var option = question.options[j];
      var selected = option.id === question.selected_option_id;
      var button = make("button", "choice-button" + (selected ? " selected" : ""), option.text_ar);
      button.type = "button";
      button.disabled = profileSaving;
      button.setAttribute("aria-pressed", selected ? "true" : "false");
      button.onclick = makeProfileChoice(question.field_id, option.id);
      options.appendChild(button);
    }
    card.appendChild(options);
    categoryNode.appendChild(card);
  }
  setText(byId("profileProgressText"), completed + " من " + profileData.required_count + " إجابة");
  setText(byId("profileProgressPercent"), Math.round(completed / profileData.required_count * 100) + "%");
  byId("profileProgressBar").style.width = Math.round(completed / profileData.required_count * 100) + "%";
  updateProfileSummary();
}

function makeProfileChoice(fieldId, optionId) {
  return function () {
    var i;
    if (!profileData || profileSaving) { return; }
    for (i = 0; i < profileData.questions.length; i++) {
      if (profileData.questions[i].field_id === fieldId) {
        profileData.questions[i].selected_option_id = optionId;
        break;
      }
    }
    if (!profileData.answers) { profileData.answers = {}; }
    profileData.answers[fieldId] = optionId;
    profileData.completed_count = Object.keys(profileData.answers).length;
    profileData.is_complete = profileData.completed_count === profileData.required_count;
    renderProfile();
    setText(byId("profileSaveState"), "جارٍ حفظ اختيارك...");
    pendingProfileAnswers[fieldId] = optionId;
    flushProfileSave();
  };
}

function flushProfileSave() {
  if (profileSaving || !room || !Object.keys(pendingProfileAnswers).length) { return; }
  var batch = pendingProfileAnswers;
  pendingProfileAnswers = {};
  profileSaving = true;
  setProfileButtonsDisabled(true);
  api("POST", "/api/rooms/" + encodeURIComponent(room.code) + "/profile", { answers: batch }, function (payload, status) {
    profileSaving = false;
    setProfileButtonsDisabled(false);
    if (status === 200 && payload.ok && payload.profile) {
      profileData = payload.profile;
      renderProfile();
      setText(byId("profileSaveState"), Object.keys(pendingProfileAnswers).length ? "جارٍ حفظ بقية الاختيارات..." : "تم حفظ إجاباتك بأمان.");
      if (Object.keys(pendingProfileAnswers).length) { flushProfileSave(); }
    } else {
      var key;
      for (key in batch) {
        if (Object.prototype.hasOwnProperty.call(batch, key) && !Object.prototype.hasOwnProperty.call(pendingProfileAnswers, key)) {
          pendingProfileAnswers[key] = batch[key];
        }
      }
      setText(byId("profileSaveState"), payload.error || "تعذر الحفظ. اختر الإجابة مرة أخرى للمحاولة.");
      message(payload.error || "تعذر حفظ إجابتك.", false);
    }
  });
}

function setProfileButtonsDisabled(disabled) {
  var nodes = byId("profileQuestions").getElementsByTagName("button");
  var i;
  for (i = 0; i < nodes.length; i++) { nodes[i].disabled = disabled; }
}

function profileBack() {
  if (Object.keys(pendingProfileAnswers).length || profileSaving) {
    setText(byId("profileSaveState"), "انتظر اكتمال حفظ إجاباتك قبل العودة.");
    return;
  }
  showView("room");
  schedule();
}

/* Match experience */
function renderScoreboard(match) {
  var list = byId("scoreboardList");
  var scores = match.scoreboard || match.final_standings || [];
  var i;
  clear(list);
  for (i = 0; i < scores.length; i++) {
    var item = make("div", "score-entry");
    item.appendChild(make("span", "score-rank", i + 1));
    item.appendChild(make("strong", "score-name", scores[i].name));
    item.appendChild(make("span", "score-points", scores[i].score));
    list.appendChild(item);
  }
}

function taskIdentity(task) {
  var clues = task.content && task.content.revealed_clues ? task.content.revealed_clues.length : 0;
  return [task.task_sequence, task.kind, task.status, task.subphase, clues,
    task.live_guess_number || 0].join(":");
}

function phaseDuration(task) {
  if (task.status === "reveal") { return 12; }
  if (task.kind === "knowledge") { return 85; }
  if (task.kind === "mystery") { return 32; }
  if (task.kind === "player_question") { return 65; }
  if (task.kind === "live_choice") { return task.subphase === "answer" ? 65 : 37; }
  return 80;
}

function updateTimer(task) {
  var remaining = Math.max(0, task.remaining_seconds || 0);
  var duration = phaseDuration(task);
  setText(byId("timerValue"), remaining + "ث");
  byId("timerBar").style.width = Math.max(0, Math.min(100, remaining / duration * 100)) + "%";
}

function renderArena(match) {
  if (!match) { return; }
  currentMatch = match;
  setText(byId("arenaPhase"), match.status === "completed" ? "انتهت المواجهة" : match.phase_label);
  setText(byId("arenaSubtitle"), match.status === "completed" ? "النتائج النهائية" : "المرحلة " + match.phase_number + " من " + match.phase_count);
  renderScoreboard(match);
  var content = byId("arenaContent");
  var task;
  if (match.status === "completed") {
    var completeKey = "completed:" + match.id;
    if (taskKey !== completeKey) {
      taskKey = completeKey;
      clear(content);
      renderFinalStandings(content, match.final_standings || match.scoreboard || []);
    }
    setText(byId("taskPhaseTag"), "الختام");
    setText(byId("taskRound"), "المباراة انتهت");
    setText(byId("timerValue"), "✓");
    byId("timerBar").style.width = "100%";
    setText(byId("arenaStatus"), "شكرًا على اللعب مع NANE.");
    return;
  }
  task = match.current_task;
  if (!task) { return; }
  updateTimer(task);
  setText(byId("taskPhaseTag"), match.phase_label);
  setText(byId("taskRound"), "الجولة " + task.round_in_phase + " / " + task.rounds_in_phase);
  var nextKey = taskIdentity(task);
  if (nextKey !== taskKey) {
    taskKey = nextKey;
    clear(content);
    if (task.status === "reveal") { renderTaskResult(content, task); }
    else { renderActiveTask(content, task, match.scoreboard || []); }
  }
  if (task.status === "reveal") {
    setText(byId("arenaStatus"), "تم كشف النتيجة. الجولة التالية تبدأ تلقائيًا.");
  } else {
    setText(byId("arenaStatus"), task.my_state && task.my_state.has_submitted ?
      "تم تسجيل اختيارك. انتظر بقية أفراد العائلة." : "اختر إجابتك قبل انتهاء الوقت.");
  }
}

function renderActiveTask(root, task, scores) {
  var content = task.content || {};
  var mine = task.my_state || {};
  var heading;
  if (task.kind === "knowledge") {
    heading = make("h2", "task-title", "من يعرفني؟");
    root.appendChild(heading);
    root.appendChild(make("p", "task-copy", "تعرّفوا على صاحب الاختيار الصحيح."));
    root.appendChild(make("div", "task-question", content.question ? content.question.text_ar : ""));
    if (mine.can_guess && !mine.has_submitted) {
      renderOptions(root, content.question ? content.question.options : [], "guess", task.task_sequence);
    } else if (mine.can_guess) {
      renderWaiting(root, "وصل اختيارك", "ننتظر بقية التخمينات حتى نكشف الإجابة.");
    } else {
      renderWaiting(root, "هذه الجولة عنك!", "راقب تخمينات العائلة واكتشف من يعرفك فعلًا.");
    }
    return;
  }

  if (task.kind === "mystery") {
    root.appendChild(make("h2", "task-title", "الرسالة المجهولة"));
    root.appendChild(make("div", "story-card"));
    var story = root.lastChild;
    story.appendChild(make("h3", "", content.title_ar || "قصة غامضة"));
    story.appendChild(make("p", "", content.story_ar || ""));
    var clues = content.revealed_clues || [];
    var clueList = make("div", "clue-list");
    var i;
    for (i = 0; i < clues.length; i++) {
      var clue = make("div", "clue-item");
      clue.appendChild(make("b", "", "تلميح " + clues[i].level));
      clue.appendChild(make("span", "", clues[i].text_ar));
      clueList.appendChild(clue);
    }
    root.appendChild(clueList);
    root.appendChild(make("p", "task-copy", "من صاحب هذه الرسالة؟ اختَر أحد أفراد العائلة."));
    if (mine.can_guess && !mine.has_submitted) {
      renderPlayers(root, scores, "guess", task.task_sequence);
    } else if (mine.can_guess) {
      renderWaiting(root, "تم تسجيل تخمينك", "قد يظهر تلميح جديد إذا لم تُحل الرسالة بعد.");
    } else {
      renderWaiting(root, "هذه الرسالة عنك", "راقب محاولات الآخرين. هويتك مخفية عنهم.");
    }
    return;
  }

  if (task.kind === "player_question") {
    root.appendChild(make("h2", "task-title", "سؤال من لاعب"));
    root.appendChild(make("p", "task-copy", "سؤال اختاره أحد أفراد العائلة ووصل سرًا إلى لاعب آخر."));
    var question = content.question || {};
    root.appendChild(make("div", "task-question", question.text_ar || ""));
    if (task.subphase === "answer") {
      if (mine.can_answer && !mine.has_submitted) {
        root.appendChild(make("p", "task-copy", "أنت المجيب هذه المرة. اختر إجابتك بصراحة."));
        renderOptions(root, question.options || [], "answer", task.task_sequence);
      } else {
        renderWaiting(root, "تم اختيار المجيب سرًا", "على المجيب المختار أن يجيب أولًا، ثم يخمّن الباقون.");
      }
    } else if (mine.can_guess && !mine.has_submitted) {
      root.appendChild(make("p", "task-copy", "ما الإجابة التي اختارها المجيب؟"));
      renderOptions(root, question.options || [], "guess", task.task_sequence);
    } else if (mine.can_guess) {
      renderWaiting(root, "تم تسجيل تخمينك", "ننتظر اكتمال التخمينات.");
    } else {
      renderWaiting(root, "إجابتك سُجلت", "العائلة تحاول الآن تخمين اختيارك.");
    }
    return;
  }

  if (task.kind === "live_choice") {
    root.appendChild(make("h2", "task-title", "من اختار هذا؟"));
    var liveQuestion = content.question || {};
    root.appendChild(make("div", "task-question small", liveQuestion.text_ar || ""));
    if (task.subphase === "answer") {
      if (mine.has_submitted) {
        renderWaiting(root, "تم حفظ اختيارك بسرية", "سنكشف الإجابات دون أسماء، واحدة تلو الأخرى.");
      } else {
        root.appendChild(make("p", "task-copy", "أجب عن السؤال. لن يعرف أحد اختيارك قبل التخمين."));
        renderOptions(root, liveQuestion.options || [], "answer", task.task_sequence);
      }
    } else {
      root.appendChild(make("p", "task-copy", "من صاحب الإجابة المجهولة رقم " + task.live_guess_number + "؟"));
      root.appendChild(make("div", "live-answer"));
      setText(root.lastChild, task.anonymous_answer || "لم تصل إجابة لهذا الدور.");
      if (mine.can_guess && !mine.has_submitted) {
        renderPlayers(root, scores, "guess", task.task_sequence, false);
      } else if (mine.can_guess) {
        renderWaiting(root, "تم تسجيل تخمينك", "ننتظر بقية اللاعبين.");
      } else {
        renderWaiting(root, "هذه إجابتك المجهولة", "راقب تخمينات الآخرين دون كشف اختيارك.");
      }
    }
    return;
  }

  if (task.kind === "finale") {
    root.appendChild(make("h2", "task-title", "المواجهة الأخيرة"));
    root.appendChild(make("p", "task-copy", "اجمعوا الخيطين… من صاحب هذين الاختيارين؟"));
    root.appendChild(make("div", "task-question", content.text_ar || ""));
    if (mine.can_guess && !mine.has_submitted) {
      renderPlayers(root, scores, "guess", task.task_sequence, false);
    } else if (mine.can_guess) {
      renderWaiting(root, "تم تسجيل تخمينك", "لحظات ونكشف صاحب الاختيارين.");
    } else {
      renderWaiting(root, "هذه المواجهة عنك", "العائلة تحاول معرفة صاحب الاختيارين.");
    }
  }
}

function renderOptions(root, options, action, sequence) {
  var list = make("div", "option-list");
  var i;
  for (i = 0; i < options.length; i++) {
    var option = options[i];
    var button = make("button", "choice-button", option.text_ar);
    button.type = "button";
    button.onclick = makeSubmit(action, option.id, sequence);
    list.appendChild(button);
  }
  root.appendChild(list);
}

function renderPlayers(root, scores, action, sequence) {
  var list = make("div", "player-choices");
  var count = 0;
  var i;
  for (i = 0; i < scores.length; i++) {
    var player = scores[i];
    var button = make("button", "choice-button", player.name);
    button.type = "button";
    button.onclick = makeSubmit(action, player.player_id, sequence);
    list.appendChild(button);
    count++;
  }
  if (count) { root.appendChild(list); }
}

function makeSubmit(action, value, sequence) {
  return function () { submitAction(action, value, sequence); };
}

function renderWaiting(root, title, detail) {
  var card = make("div", "waiting-card");
  card.appendChild(make("strong", "", title));
  card.appendChild(make("p", "", detail));
  root.appendChild(card);
}

function submitAction(action, value, sequence) {
  if (!room || !currentMatch || !currentMatch.current_task) { return; }
  var current = currentMatch.current_task;
  if (current.task_sequence !== sequence || current.status !== "active") {
    setText(byId("arenaStatus"), "تغيّرت الجولة. جارٍ تحديث الشاشة...");
    refresh();
    return;
  }
  setText(byId("arenaStatus"), "جارٍ تسجيل اختيارك...");
  api("POST", "/api/rooms/" + encodeURIComponent(room.code) + "/match/action", {
    action: action,
    value: value,
    task_sequence: sequence
  }, function (payload, status) {
    if (status === 200 && payload.ok && payload.match) {
      currentMatch = payload.match;
      taskKey = "";
      renderArena(currentMatch);
    } else {
      message(payload.error || "تعذر تسجيل الاختيار.", false);
      refresh();
    }
  });
}

function renderTaskResult(root, task) {
  var result = task.result || {};
  var card = make("div", "result-card");
  var title = "انكشفت الإجابة!";
  if (result.type === "mystery") { title = "انكشف صاحب الرسالة"; }
  if (result.type === "live_choice") { title = "انكشف أصحاب الإجابات"; }
  if (result.type === "finale") { title = "حُسمت المواجهة الأخيرة"; }
  card.appendChild(make("h3", "", title));
  if (result.target_name) { card.appendChild(make("p", "", "صاحب الاختيار: " + result.target_name)); }
  if (result.asker_name) { card.appendChild(make("p", "", "صاحب السؤال: " + result.asker_name)); }
  if (result.question) { card.appendChild(make("p", "", result.question)); }
  if (result.answer) { card.appendChild(make("span", "result-answer", result.answer)); }
  if (result.title_ar) { card.appendChild(make("p", "", result.title_ar)); }
  if (result.story_ar) { card.appendChild(make("p", "", result.story_ar)); }
  if (result.skipped) { card.appendChild(make("p", "", "لم تُسجل إجابة في هذه الجولة.")); }
  if (result.facts && result.facts.length) {
    var facts = make("ul", "result-facts");
    var i;
    for (i = 0; i < result.facts.length; i++) {
      var fact = result.facts[i];
      facts.appendChild(make("li", "", typeof fact === "string" ? fact : (fact.answer || "")));
    }
    card.appendChild(facts);
  }
  if (result.answers && result.answers.length) {
    var answers = make("div", "live-results");
    var j;
    for (j = 0; j < result.answers.length; j++) {
      var answer = result.answers[j];
      var row = make("div", "result-row");
      row.appendChild(make("span", "", answer.answer || "دون إجابة"));
      row.appendChild(make("strong", "", answer.owner_name));
      answers.appendChild(row);
    }
    card.appendChild(answers);
  }
  var changes = result.score_changes || [];
  if (changes.length) {
    var points = make("div", "result-changes");
    points.appendChild(make("strong", "", "نقاط هذه الجولة"));
    var k;
    for (k = 0; k < changes.length; k++) {
      var change = changes[k];
      points.appendChild(make("div", "result-row", (change.player_name || "لاعب") + "  +" + change.points));
    }
    card.appendChild(points);
  } else {
    card.appendChild(make("div", "result-changes", "لم تُسجّل نقاط في هذه الجولة."));
  }
  root.appendChild(card);
  root.appendChild(make("p", "task-copy", "استعدوا للجولة التالية…"));
}

function renderFinalStandings(root, standings) {
  var card = make("div", "finish-card");
  card.appendChild(make("h2", "", "أحسنتم يا عائلة!"));
  card.appendChild(make("p", "", "انتهت المراحل الخمس. هذا هو الترتيب النهائي."));
  root.appendChild(card);
  var i;
  for (i = 0; i < standings.length; i++) {
    var row = make("div", "result-row");
    row.appendChild(make("span", "", (i + 1) + ". " + standings[i].name));
    row.appendChild(make("strong", "", standings[i].score + " نقطة"));
    root.appendChild(row);
  }
  root.appendChild(make("p", "task-copy", "شكرًا لمشاركتكم. يمكنكم بدء مباراة جديدة بعد العودة إلى الغرفة."));
  var back = make("button", "button secondary", "العودة إلى الغرفة");
  back.type = "button";
  back.onclick = function () {
    dismissedMatchId = currentMatch ? currentMatch.id : null;
    taskKey = "";
    showView("room");
    if (room) { renderRoom(room); }
    schedule();
  };
  root.appendChild(back);
}

function boot() {
  var code = roomQuery();
  if (code) { byId("roomCode").value = code; }
  api("GET", "/api/health", null, function (payload, status) {
    online(status === 200 && payload.ok);
  });
  api("GET", "/api/me", null, function (payload, status) {
    if (status === 200 && payload.ok) { enter(payload); }
    else { showView("entry"); }
  });
}

byId("createButton").onclick = createRoom;
byId("joinButton").onclick = joinRoom;
byId("readyButton").onclick = ready;
byId("leaveButton").onclick = leave;
byId("copyButton").onclick = copyCode;
byId("profileButton").onclick = showProfile;
byId("profileBack").onclick = profileBack;
byId("profileDone").onclick = profileBack;
byId("startButton").onclick = startMatch;
byId("roomCode").onkeyup = function () { this.value = this.value.toUpperCase(); };

if (window.addEventListener) {
  window.addEventListener("online", function () {
    online(true);
    if (room) { refresh(); } else { boot(); }
  }, false);
  window.addEventListener("offline", function () { online(false); }, false);
  window.addEventListener("beforeunload", stopPolling, false);
}

boot();
}());
