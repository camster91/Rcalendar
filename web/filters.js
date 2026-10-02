/* Helpers shared by calendar.html and list.html.
 *
 * Both pages grew their own copy of these, and the copies drifted: list.html's
 * fmtDay lacked the full-timestamp guard that keeps a copied card off "Invalid
 * Date", and its cardHTML had no all-day branch, so an all-day block rendered
 * as a 23-hour booking. One copy is the fix; a third page would otherwise make
 * it three.
 *
 * Loaded as a classic script, so everything here is a global and the two pages
 * can call it directly. That also means top-level `const` names are shared:
 * declaring PAL, DAYS or MONTHS in a page as well is a parse-time error
 * ("Identifier 'PAL' has already been declared") which kills the page blank
 * rather than degrading, so they are declared here and nowhere else.
 *
 * Nothing here touches the DOM or reads a page's own state, except staleTab
 * at the bottom, whose banner is the same on both pages. Anything that needs
 * the room list takes it as an argument, which is what keeps this file honest
 * about what it depends on.
 */

// Room colours. These fill month chips, week bars, card borders and the room
// dots, and every chip carries white text, so each one has to clear 4.5:1
// against white — the previous set was Tailwind 500 and 15 of the 16 sat
// between 1.9 and 4.5. Same hues, deepened just far enough to pass.
//
// Assigned by the room's position in the list, so neighbouring rooms get
// different colours and the assignment is stable across reloads. Falls back to
// the first character for a room not in the list, which at least keeps it
// consistent from one render to the next.
const PAL = ["#1b6df5","#e81414","#0c855d","#a26807","#8453f5","#df177a","#048096","#c35305","#0e8376","#6063f1","#54810e","#c513e0","#0b7cb0","#178640","#937005","#64748b"];

const DAYS=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'],
      MONTHS=['January','February','March','April','May','June','July','August','September','October','November','December'];

function rcol(r, rooms){
  const list=rooms||[];
  const i=list.indexOf(r);
  return PAL[(i<0?r.charCodeAt(0):i)%PAL.length];
}

function fmtT(iso){if(!iso)return'';const d=new Date(iso);return d.toLocaleTimeString('en-CA',{hour:'numeric',minute:'2-digit',hour12:true});}
// Duration from the wall-clock fields, not the millisecond delta: a booking
// spanning the spring-forward hour is 8 wall-clock hours but 7 real ones, and
// the parenthetical should agree with the times printed next to it. Times
// here are naive local ISO, so the HH:MM pair is the whole truth.
function fmtDur(s,e){if(!s||!e)return'';const t=x=>{const m=/T(\d{2}):(\d{2})/.exec(x);return m?(+m[1])*60+(+m[2]):null;};
  const a=t(s),b=t(e);if(a===null||b===null||b<a)return'';
  const m=b-a,h=Math.floor(m/60),mm=m%60;
  return h>0?(mm>0?h+'h '+mm+'m':h+'h'):mm+'m';}
// Takes either a bare date ('2026-09-17') or a full ISO timestamp. The
// T12:00:00 is only for the bare-date case, where it keeps the parse out of
// UTC rounding either side of midnight; appending it to an already-full
// timestamp produced "Invalid Date" — which is what copyCard had been putting
// on the clipboard.
function fmtDay(iso){if(!iso)return'';const d=new Date(iso.length>10?iso:iso+'T12:00:00');return d.toLocaleDateString('en-CA',{weekday:'short',month:'short',day:'numeric'});}
function esc(s){return(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

// A booking that travels through markup as a data attribute. Both pages hang
// their copy button off one, and the pair exists so the encoding and its
// decoding cannot drift.
//
// The copy button used to be an onclick attribute holding JSON.stringify(ev)
// with only the double quote escaped. That is not enough: the HTML tokenizer
// decodes character references inside an attribute *before* the result is
// compiled as script, so a title containing the five characters &quot; became a
// real quote, broke out of the JSON string, and ran as code. A title is free
// text that anyone who books a Rotman room can set, and the code would run in
// this app's origin — which on a loopback app with no authentication is the
// whole control plane.
//
// encodeURIComponent escapes everything but the unreserved set, so the value
// cannot carry a quote, an angle bracket or an ampersand into the attribute;
// esc() then escapes what markup sees. Nothing but data crosses the boundary.
function evAttr(ev){return esc(encodeURIComponent(JSON.stringify(ev)));}
function evFromAttr(raw){try{return JSON.parse(decodeURIComponent(raw||''));}catch(_){return null;}}

// A booking's title as the UI prints it: the event name, with the who/what
// that booked it (the server's `booked_by`, parsed out of LSM's
// "208/NAME/BOOKER" title) inline and dimmed. One helper so the two pages
// print the same thing; the inline style matches the dim-annotation idiom
// the cards already use.
function titleHTML(ev){
  return esc(ev.title||'')+(ev.booked_by
    ?' <span style="color:var(--tx3);font-size:.82rem;font-weight:normal">— '
     +esc(ev.booked_by)+'</span>'
    :'');
}

// One row per booking, with `rooms` listing every room it occupies. Bookings
// that share a time, a title and a description are the same class in several
// rooms, and the pages show them as one card. The booker is part of the key:
// same name and time but different bookers is two bookings, not one class.
function mergeEvts(evts){
  const m=new Map();
  evts.forEach(e=>{const k=(e.start||'')+'|'+(e.end||'')+'|'+(e.title||'')+'|'+(e.booked_by||'')+'|'+(e.description||'');if(m.has(k))m.get(k).rooms.push(e.room);else m.set(k,{...e,rooms:[e.room]});});
  return Array.from(m.values());
}

// ── URL / PRESET FIELDS ──
// Coerce an untrusted bag of filter values into the shape the state variables
// expect. One answer for two callers: readURL, whose values are strings off the
// query string, and applyPreset, whose values are whatever JSON is on disk.
//
// applyPreset used to assign straight through, and a preset is a user file that
// can hold anything. floors:"Ground Floor" became new Set("Ground Floor") — a
// set of individual *characters*, matching no room's floor, which blanked the
// calendar with no error to explain it. A non-array rooms threw on .filter and
// left the rest half-applied. Both are handled here rather than at the call
// site, because the bad presets are already on disk: validating future writes
// on the server would not rescue the ones already saved.
//
// ctx carries the names that exist: {rooms: [...], groups: {...}}.
function parseFilters(raw, ctx){
  const rooms=(ctx&&ctx.rooms)||[], groups=(ctx&&ctx.groups)||{};
  const src=(raw&&typeof raw==='object')?raw:{};
  // A list of names, from an array or a comma-joined string, keeping only
  // strings and intersecting with what actually exists — so a name that is no
  // longer in the data drops out instead of selecting nothing. An absent *or
  // empty* list returns null, which every caller reads as "no opinion", the
  // same rule roomClear documents. Empty is judged *after* the intersection:
  // judged before, a link naming only rooms that have since gone came back as
  // [] — a selection of nothing, which blanked the page under "All Rooms".
  const names=(v,valid)=>{
    let list=null;
    if(Array.isArray(v))list=v.filter(x=>typeof x==='string'&&x);
    else if(typeof v==='string'&&v)list=v.split(',').filter(Boolean);
    if(list&&valid)list=list.filter(x=>valid.indexOf(x)>=0);
    return list&&list.length?list:null;
  };
  return {
    rooms:names(src.rooms,rooms),
    groups:names(src.groups,Object.keys(groups)),
    q:typeof src.q==='string'?src.q.toLowerCase():'',
    floors:names(src.floors,null),
    // +x||0 turns a non-number into 0 rather than NaN, which would poison
    // every comparison it reached.
    seats:Math.max(0,+src.seats||0),
    panopto:src.panopto===true||src.panopto===1||src.panopto==='1',
    free:typeof src.free==='string'&&/^\d{2}:\d{2}$/.test(src.free)?src.free:'',
    // Clamped to a day: the longest the question is meaningful for, and the
    // same cap the server puts on `for`.
    mins:Math.min(1440,Math.max(1,+src.mins||60)),
  };
}

// The rooms a group selection stands for, limited to rooms the data has.
// Both pages use this one: a group is shorthand for its rooms everywhere,
// so a link naming a group means the same bookings on either page.
function groupRooms(groups, groupMap, rooms){
  const out=new Set();
  groups.forEach(g=>(groupMap[g]||[]).forEach(r=>{if(rooms.includes(r))out.add(r);}));
  return out;
}

// What choosing a room does to the room selection — one set of rules for both
// pages, so a click means the same thing wherever it lands. "Every room" is
// the full set, and no rule ever returns an empty one: an empty selection is
// a blank calendar with no way back, so each path that could empty it falls
// back to every room instead.
//   click  — select just this room; clicking the room that already is the
//            whole selection undoes that, back to every room.
//   toggle — add or remove this one room (Ctrl/⌘/Shift-click).
//   pick   — from a search suggestion or a free-room row: with every room
//            on, narrow to this one; with a narrower selection, add it.
//   only   — exactly this room (a room dropdown).
//   remove — drop this room (a tag's ✕).
//   clear  — every room.
// A room the data does not have changes nothing.
function nextRooms(current, room, mode, rooms){
  const all=()=>new Set(rooms);
  if(mode==='clear')return all();
  const cur=new Set(current);
  if(!rooms.includes(room))return cur;
  if(mode==='only')return new Set([room]);
  if(mode==='click')return cur.size===1&&cur.has(room)?all():new Set([room]);
  if(mode==='pick'){
    if(cur.size>=rooms.length)return new Set([room]);
    cur.add(room);return cur;
  }
  if(mode==='toggle'&&!cur.has(room)){cur.add(room);return cur;}
  if(mode==='toggle'||mode==='remove'){
    cur.delete(room);
    return cur.size?cur:all();
  }
  return cur;
}

// ── STALE TAB ──
// The server answers every /api/ call 401 once this tab's key cookie is from
// an earlier launch — the app restarted or updated under an open tab (see
// _require_key in app/server.py). Nothing in the tab can mend that: the key
// is traded only on the URL the tray opens. Read as anything else it misled:
// the boot card offered an LSM sign-in, Sign in toasted "a browser window
// opened" and polled for six minutes, and Scrape spun for fifteen. So a 401 is
// said once, in words that name the fix, and TAB_STALE stops every poll.
//
// Pass any fetch Response; true means this tab is stale and the caller should
// stop. Built with DOM calls and textContent, like the rest of the UI.
const STALE_MSG='This tab is out of date — reopen the calendar from the tray icon or Start menu.';
let TAB_STALE=false;
function staleTab(r){
  if(!TAB_STALE&&r&&r.status===401){
    TAB_STALE=true;
    const b=document.createElement('div');
    b.id='staleBanner';b.setAttribute('role','alert');b.textContent=STALE_MSG;
    // Fixed over the toolbar: nothing on it works from this tab any more.
    // #dc1313 is the pages' --rd, which carries white text at 5:1.
    b.style.cssText='position:fixed;top:0;left:0;right:0;z-index:10000;padding:14px 20px;'
      +'background:#dc1313;color:#fff;font:600 .95rem Inter,system-ui,sans-serif;text-align:center';
    document.body.appendChild(b);
  }
  return TAB_STALE;
}
// The same check as a throw, for a fetch inside a try whose catch already
// says what went wrong and puts its button back: keyed(await fetch(url)).
function keyed(r){if(staleTab(r))throw new Error(STALE_MSG);return r;}
