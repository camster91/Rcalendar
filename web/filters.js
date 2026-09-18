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
 * Nothing here touches the DOM or reads a page's own state. Anything that
 * needs the room list takes it as an argument, which is what keeps this file
 * honest about what it depends on.
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
function fmtDur(s,e){if(!s||!e)return'';const ms=new Date(e)-new Date(s);const h=Math.floor(ms/36e5);const m=Math.round((ms%36e5)/6e4);return h>0?(m>0?h+'h '+m+'m':h+'h'):(m+'m');}
// Takes either a bare date ('2026-09-17') or a full ISO timestamp. The
// T12:00:00 is only for the bare-date case, where it keeps the parse out of
// UTC rounding either side of midnight; appending it to an already-full
// timestamp produced "Invalid Date" — which is what copyCard had been putting
// on the clipboard.
function fmtDay(iso){if(!iso)return'';const d=new Date(iso.length>10?iso:iso+'T12:00:00');return d.toLocaleDateString('en-CA',{weekday:'short',month:'short',day:'numeric'});}
function esc(s){return(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

// One row per booking, with `rooms` listing every room it occupies. Bookings
// that share a time, a title and a description are the same class in several
// rooms, and the pages show them as one card.
function mergeEvts(evts){
  const m=new Map();
  evts.forEach(e=>{const k=(e.start||'')+'|'+(e.end||'')+'|'+(e.title||'')+'|'+(e.description||'');if(m.has(k))m.get(k).rooms.push(e.room);else m.set(k,{...e,rooms:[e.room]});});
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
  // same rule roomClear documents.
  const names=(v,valid)=>{
    let list=null;
    if(Array.isArray(v))list=v.filter(x=>typeof x==='string'&&x);
    else if(typeof v==='string'&&v)list=v.split(',').filter(Boolean);
    if(!list||!list.length)return null;
    return valid?list.filter(x=>valid.indexOf(x)>=0):list;
  };
  return {
    rooms:names(src.rooms,rooms),
    groups:names(src.groups,Object.keys(groups)),
    q:typeof src.q==='string'?src.q:'',
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
