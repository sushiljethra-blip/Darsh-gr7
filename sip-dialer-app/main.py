#!/usr/bin/env python3
"""
BPO SIP Auto-Dialer Pro — Desktop Edition
Works with Elastix 2.5 / Asterisk over standard UDP SIP (port 5060)
No PBX modifications required.

Requirements:
    pip install pyvoip PyAudio numpy

Build EXE:
    pip install pyinstaller
    pyinstaller --onefile --windowed --name BPO-SIP-Dialer main.py
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font as tkfont
import threading
import time
import csv
import sqlite3
import json
import os
import sys
import re
import io
import datetime
import urllib.request
import urllib.error
import queue

# ── Optional heavy deps ────────────────────────────────────────────────────
try:
    from pyvoip.voip import VoIPPhone
    from pyvoip.call import CallState
    HAS_SIP = True
except ImportError:
    HAS_SIP = False
    CallState = None

try:
    import pyaudio
    import numpy as np
    HAS_AUDIO = True
except ImportError:
    HAS_AUDIO = False

# ── Paths ──────────────────────────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

DB_PATH  = os.path.join(APP_DIR, "dialer_notes.db")
CFG_PATH = os.path.join(APP_DIR, "dialer_config.json")

# ── Colours ────────────────────────────────────────────────────────────────
C = {
    'bg':     '#1a1b26',
    'bg1':    '#24283b',
    'bg2':    '#2a2f45',
    'bg3':    '#313652',
    'border': '#3d4466',
    'txt0':   '#c0caf5',
    'txt1':   '#9aa5ce',
    'txt2':   '#565f89',
    'blue':   '#7aa2f7',
    'green':  '#9ece6a',
    'red':    '#f7768e',
    'yellow': '#e0af68',
    'purple': '#bb9af7',
    'cyan':   '#7dcfff',
    'orange': '#ff9e64',
}

# ══════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════
class Config:
    DEFAULTS = {
        'server': '', 'port': '5060', 'username': '', 'password': '',
        'display_name': 'Agent', 'local_sip_port': '5080',
        'dial_delay': '5', 'auto_dial': False,
    }
    def __init__(self):
        self._d = dict(self.DEFAULTS)
        self._load()

    def _load(self):
        try:
            with open(CFG_PATH, encoding='utf-8') as f:
                self._d.update(json.load(f))
        except Exception:
            pass

    def save(self):
        try:
            with open(CFG_PATH, 'w', encoding='utf-8') as f:
                json.dump(self._d, f, indent=2)
        except Exception:
            pass

    def __getitem__(self, k):  return self._d.get(k, self.DEFAULTS.get(k, ''))
    def __setitem__(self, k, v): self._d[k] = v
    def get(self, k, d=None):  return self._d.get(k, d)

# ══════════════════════════════════════════════════════════════════════════
#  NOTES DATABASE
# ══════════════════════════════════════════════════════════════════════════
class NotesDB:
    def __init__(self):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_key   TEXT    NOT NULL,
                created_at  TEXT    NOT NULL,
                outcome     TEXT,
                note        TEXT,
                source      TEXT DEFAULT 'manual'
            )""")
        self._conn.commit()

    def add(self, phone_key, note='', outcome='note', source='manual'):
        with self._lock:
            self._conn.execute(
                "INSERT INTO notes (phone_key,created_at,outcome,note,source) "
                "VALUES (?,?,?,?,?)",
                (phone_key, datetime.datetime.now().strftime('%d %b %Y %H:%M'),
                 outcome, note, source)
            )
            self._conn.commit()

    def get(self, phone_key, limit=25):
        with self._lock:
            cur = self._conn.execute(
                "SELECT created_at,outcome,note FROM notes "
                "WHERE phone_key=? ORDER BY id DESC LIMIT ?",
                (phone_key, limit)
            )
            return cur.fetchall()

    def close(self):
        self._conn.close()

# ══════════════════════════════════════════════════════════════════════════
#  AUDIO MANAGER
# ══════════════════════════════════════════════════════════════════════════
class AudioManager:
    CHUNK = 160   # 20ms at 8kHz
    RATE  = 8000
    SILENCE = bytes(CHUNK * 2)

    def __init__(self):
        self._pa  = None
        self._in  = None
        self._out = None
        self._thr = None
        self._run = False
        self.muted = False

    # ── public ──────────────────────────────────────────────────────────
    def start(self, call):
        self.stop()
        if not HAS_AUDIO:
            return
        self._open()
        self._run = True
        self._thr = threading.Thread(
            target=self._single_loop, args=(call,), daemon=True)
        self._thr.start()

    def start_conference(self, call1, call2):
        self.stop()
        if not HAS_AUDIO:
            return
        self._open()
        self._run = True
        self._thr = threading.Thread(
            target=self._conf_loop, args=(call1, call2), daemon=True)
        self._thr.start()

    def stop(self):
        self._run = False
        if self._thr:
            self._thr.join(timeout=1.0)
            self._thr = None
        for s in (self._in, self._out):
            if s:
                try: s.stop_stream(); s.close()
                except Exception: pass
        self._in = self._out = None
        if self._pa:
            try: self._pa.terminate()
            except Exception: pass
            self._pa = None

    # ── private ─────────────────────────────────────────────────────────
    def _open(self):
        self._pa = pyaudio.PyAudio()
        cfg = dict(format=pyaudio.paInt16, channels=1,
                   rate=self.RATE, frames_per_buffer=self.CHUNK)
        self._in  = self._pa.open(input=True,  **cfg)
        self._out = self._pa.open(output=True, **cfg)

    def _mic(self):
        if self.muted or not self._in:
            return self.SILENCE
        try:
            return self._in.read(self.CHUNK, exception_on_overflow=False)
        except Exception:
            return self.SILENCE

    def _play(self, data):
        if self._out:
            try: self._out.write(data)
            except Exception: pass

    def _rtp_read(self, call):
        try: return call.read_audio(self.CHUNK, block=False) or self.SILENCE
        except Exception: return self.SILENCE

    def _single_loop(self, call):
        while self._run:
            try:
                call.write_audio(self._mic())
                rtp = self._rtp_read(call)
                if rtp != self.SILENCE:
                    self._play(rtp)
            except Exception:
                pass
            time.sleep(0.001)

    def _conf_loop(self, c1, c2):
        while self._run:
            try:
                mic = np.frombuffer(self._mic(),       dtype=np.int16).astype(np.int32)
                r1  = np.frombuffer(self._rtp_read(c1), dtype=np.int16).astype(np.int32)
                r2  = np.frombuffer(self._rtp_read(c2), dtype=np.int16).astype(np.int32)

                def mix(*arrs):
                    return np.clip(sum(arrs), -32768, 32767).astype(np.int16).tobytes()

                c1.write_audio(mix(r2, mic))   # party 1 hears party 2 + mic
                c2.write_audio(mix(r1, mic))   # party 2 hears party 1 + mic
                self._play(mix(r1, r2))        # agent hears both parties
            except Exception:
                pass
            time.sleep(0.001)

# ══════════════════════════════════════════════════════════════════════════
#  SIP MANAGER
# ══════════════════════════════════════════════════════════════════════════
class SIPManager:
    """Thin wrapper around pyVoIP — uses standard UDP SIP, works with
       Elastix 2.5 / Asterisk without any PBX changes."""

    def __init__(self, ui_queue: queue.Queue):
        self._q    = ui_queue
        self._ph   = None     # VoIPPhone
        self._call = None     # primary VoIPCall
        self._conf = None     # conference VoIPCall
        self._att  = None     # attended-transfer VoIPCall
        self._audio = AudioManager()
        self.state  = 'idle'
        self.muted  = False
        self._server = ''

    # ── emit helper ────────────────────────────────────────────────────
    def _emit(self, event, data=None):
        self._q.put((event, data))

    # ── connect / disconnect ────────────────────────────────────────────
    def connect(self, server, port, user, password, sip_port=5080):
        if not HAS_SIP:
            raise RuntimeError(
                "pyVoIP not installed.\nRun:  pip install pyvoip")
        self.disconnect()
        self._server = server

        def _incoming(c):
            self._emit('incoming', c)

        self._ph = VoIPPhone(
            server=server,
            port=int(port),
            user=user,
            password=password,
            callCallback=_incoming,
            sipPort=int(sip_port),
            rtpPortLow=10000,
            rtpPortHigh=10100,
        )
        self._ph.start()
        self.state = 'registered'
        self._emit('registered', f"{user}@{server}:{port}")

    def disconnect(self):
        self._hangup_call(self._call)
        self._hangup_call(self._conf)
        self._hangup_call(self._att)
        self._call = self._conf = self._att = None
        self._audio.stop()
        if self._ph:
            try: self._ph.stop()
            except Exception: pass
            self._ph = None
        self.state = 'idle'
        self._emit('disconnected')

    # ── outbound call ──────────────────────────────────────────────────
    def dial(self, number):
        if not self._ph:
            raise RuntimeError("Not connected to SIP server")
        if self.state not in ('registered', 'idle'):
            raise RuntimeError("Already in a call")
        self._call = self._ph.call(number)
        self.state = 'calling'
        self._emit('calling', number)
        threading.Thread(
            target=self._watch, args=(self._call, 'primary'),
            daemon=True).start()

    def answer(self, incoming_call):
        incoming_call.answer()
        self._call = incoming_call
        self.state = 'calling'
        self._emit('calling', 'incoming')
        threading.Thread(
            target=self._watch, args=(incoming_call, 'primary'),
            daemon=True).start()

    def hangup(self):
        self._audio.stop()
        self._hangup_call(self._call)
        self._hangup_call(self._conf)
        self._hangup_call(self._att)
        self._call = self._conf = self._att = None
        self.state = 'registered'

    def hold(self):
        if self._call:
            try: self._call.hold()
            except Exception: pass

    def unhold(self):
        if self._call:
            try: self._call.unhold()
            except Exception: pass

    def toggle_mute(self):
        self.muted = not self.muted
        self._audio.muted = self.muted
        return self.muted

    def dtmf(self, digit):
        if self._call:
            try: self._call.send_dtmf(digit)
            except Exception: pass

    # ── conference ─────────────────────────────────────────────────────
    def add_conference(self, number):
        if not self._ph or not self._call:
            return
        self.hold()
        self._conf = self._ph.call(number)
        threading.Thread(
            target=self._watch, args=(self._conf, 'conf'),
            daemon=True).start()

    # ── transfer ───────────────────────────────────────────────────────
    def blind_transfer(self, number):
        if not self._call:
            return
        target = number if '@' in number else f"{number}@{self._server}"
        try:
            self._call.transfer(f"sip:{target}")
        except AttributeError:
            # pyVoIP version without transfer() — do BYE + re-INVITE approach
            self._emit('error', "This pyVoIP version does not support REFER transfer.\n"
                                "Upgrade: pip install --upgrade pyvoip")
        except Exception as e:
            self._emit('error', str(e))

    def attended_transfer_start(self, number):
        if not self._ph or not self._call:
            return
        self.hold()
        self._att = self._ph.call(
            number if '@' in number else f"{number}@{self._server}")
        threading.Thread(
            target=self._watch, args=(self._att, 'attended'),
            daemon=True).start()

    def attended_transfer_complete(self):
        if not self._call or not self._att:
            return
        try:
            self._call.transfer(self._att)
        except Exception as e:
            self._emit('error', str(e))
        self._att = None
        self.hangup()

    def attended_transfer_cancel(self):
        self._hangup_call(self._att)
        self._att = None
        self.unhold()

    # ── internal ───────────────────────────────────────────────────────
    def _watch(self, call, role):
        prev = None
        while True:
            try:
                s = call.state
            except Exception:
                break
            if s != prev:
                if s == CallState.ANSWERING:
                    if role == 'primary':
                        self._emit('ringing')
                    elif role in ('conf', 'attended'):
                        self._emit(f'{role}_ringing')

                elif s == CallState.ANSWERED:
                    if role == 'primary':
                        self.state = 'active'
                        self._emit('answered')
                        self._audio.start(call)
                    elif role == 'conf':
                        self.unhold()
                        self.state = 'conference'
                        self._emit('conf_answered')
                        self._audio.start_conference(self._call, self._conf)
                    elif role == 'attended':
                        self._emit('attended_answered')

                elif s == CallState.ENDED:
                    if role == 'primary':
                        self._audio.stop()
                        self._call = None
                        self.state = 'registered'
                        self._emit('ended')
                    elif role == 'conf':
                        self._conf = None
                        if self.state == 'conference':
                            self.state = 'active'
                            self._emit('conf_ended')
                    elif role == 'attended':
                        self._att = None
                        self._emit('attended_ended')
                    break
                prev = s
            time.sleep(0.08)

    @staticmethod
    def _hangup_call(call):
        if call:
            try: call.hangup()
            except Exception: pass

# ══════════════════════════════════════════════════════════════════════════
#  CONTACT MANAGER
# ══════════════════════════════════════════════════════════════════════════
def clean_phone(raw):
    return re.sub(r'\s+', '', str(raw or '').strip())

def contact_key(contact):
    return re.sub(r'\D', '', contact.get('phone', '')) or contact.get('name', 'unknown')

def string_colour(s):
    h = 0
    for c in (s or ''):
        h = (h * 31 + ord(c)) & 0xFFFFFF
    hue = abs(h) % 360
    # Return hex colour with fixed sat/lum
    import colorsys
    r, g, b = colorsys.hls_to_rgb(hue/360, 0.55, 0.55)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

STATUS_DONE = {'completed','answered','noanswer','busy','callback','dnc','failed','skipped'}

# ══════════════════════════════════════════════════════════════════════════
#  MAIN APPLICATION WINDOW
# ══════════════════════════════════════════════════════════════════════════
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("BPO SIP Auto-Dialer Pro")
        self.geometry("1200x720")
        self.minsize(900, 600)
        self.configure(bg=C['bg'])

        # State
        self.cfg      = Config()
        self.notes_db = NotesDB()
        self._ui_q    = queue.Queue()
        self.sip      = SIPManager(self._ui_q)
        self.contacts = []
        self.filtered = []
        self.cur_idx  = -1
        self.outcome  = None
        self.queue_running  = False
        self.auto_dial      = tk.BooleanVar(value=self.cfg['auto_dial'])
        self._cd_job        = None   # countdown after() id
        self._cd_remain     = 0
        self._timer_job     = None
        self._call_secs     = 0
        self._hold_state    = False
        self._mute_state    = False
        self._incoming_call = None

        self._apply_style()
        self._build_ui()
        self._poll_ui_queue()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ──────────────────────────────────────────────────────────────────
    #  STYLE
    # ──────────────────────────────────────────────────────────────────
    def _apply_style(self):
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('.', background=C['bg'], foreground=C['txt0'],
                        fieldbackground=C['bg1'], bordercolor=C['border'],
                        troughcolor=C['bg1'], selectbackground=C['blue'],
                        selectforeground=C['bg'], font=('Segoe UI', 9))
        style.configure('TFrame',  background=C['bg'])
        style.configure('TLabel',  background=C['bg'],  foreground=C['txt0'])
        style.configure('TButton', background=C['bg2'], foreground=C['txt0'],
                        bordercolor=C['border'], padding=(8,4))
        style.map('TButton',
                  background=[('active', C['bg3']), ('pressed', C['bg3'])],
                  bordercolor=[('active', C['blue'])])
        style.configure('Primary.TButton', background=C['blue'],
                        foreground=C['bg'], font=('Segoe UI', 9, 'bold'))
        style.map('Primary.TButton',
                  background=[('active', '#5d8ef0'), ('pressed', '#4a7de0')])
        style.configure('Success.TButton', background=C['green'],
                        foreground=C['bg'], font=('Segoe UI', 9, 'bold'))
        style.map('Success.TButton',
                  background=[('active', '#86be50')])
        style.configure('Danger.TButton', background=C['red'],
                        foreground=C['bg'], font=('Segoe UI', 9, 'bold'))
        style.map('Danger.TButton',
                  background=[('active', '#e55a6e')])
        style.configure('TEntry', fieldbackground=C['bg2'],
                        foreground=C['txt0'], bordercolor=C['border'])
        style.configure('TCombobox', fieldbackground=C['bg2'],
                        foreground=C['txt0'])
        style.configure('TCheckbutton', background=C['bg'],
                        foreground=C['txt1'])
        style.configure('Vertical.TScrollbar', background=C['bg2'],
                        troughcolor=C['bg1'], arrowcolor=C['txt2'])

    # ──────────────────────────────────────────────────────────────────
    #  BUILD UI
    # ──────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # ── top bar ──────────────────────────────────────────────────
        bar = tk.Frame(self, bg=C['bg1'], height=46)
        bar.pack(fill='x', side='top')
        bar.pack_propagate(False)

        tk.Label(bar, text="📞  BPO Auto-Dialer Pro",
                 bg=C['bg1'], fg=C['txt0'],
                 font=('Segoe UI', 11, 'bold')).pack(side='left', padx=14)

        # SIP status pill
        self.sip_frame = tk.Frame(bar, bg=C['bg2'],
                                  bd=1, relief='solid', padx=10, pady=2)
        self.sip_frame.pack(side='left', padx=10)
        self.sip_dot   = tk.Label(self.sip_frame, text='●', bg=C['bg2'],
                                   fg=C['txt2'], font=('Segoe UI', 9))
        self.sip_dot.pack(side='left')
        self.sip_lbl   = tk.Label(self.sip_frame, bg=C['bg2'], fg=C['txt1'],
                                   font=('Segoe UI', 9),
                                   text='Not Connected — Configure SIP')
        self.sip_lbl.pack(side='left', padx=(4,0))

        right_bar = tk.Frame(bar, bg=C['bg1'])
        right_bar.pack(side='right', padx=10)
        self._btn(right_bar, '⚙  SIP Settings', self._open_settings).pack(
            side='right', padx=3)
        self.conf_btn = self._btn(right_bar, '🤝  Conference',
                                   self._open_conference, state='disabled')
        self.conf_btn.pack(side='right', padx=3)
        self.xfer_btn = self._btn(right_bar, '↗  Transfer',
                                   self._open_transfer, state='disabled')
        self.xfer_btn.pack(side='right', padx=3)

        # ── three-panel body ─────────────────────────────────────────
        body = tk.Frame(self, bg=C['bg'])
        body.pack(fill='both', expand=True)

        # weights: left=2, centre=3, right=2.5
        body.columnconfigure(0, weight=20, minsize=230)
        body.columnconfigure(1, weight=30, minsize=300)
        body.columnconfigure(2, weight=25, minsize=260)
        body.rowconfigure(0, weight=1)

        sep_kw = dict(bg=C['border'], width=1)
        self._left_panel(body).grid(  row=0, col=0, sticky='nsew')
        tk.Frame(body, **sep_kw).grid(row=0, col=0, sticky='nse')
        self._center_panel(body).grid(row=0, col=1, sticky='nsew')
        tk.Frame(body, **sep_kw).grid(row=0, col=1, sticky='nse')
        self._right_panel(body).grid( row=0, col=2, sticky='nsew')

    # ──────────────────────────────────────────────────────────────────
    #  LEFT PANEL — CONTACT QUEUE
    # ──────────────────────────────────────────────────────────────────
    def _left_panel(self, parent):
        f = tk.Frame(parent, bg=C['bg'])

        hdr = self._panel_header(f, "📋  Contact Queue")
        clear_btn = self._btn(hdr, 'Clear', self._clear_queue, small=True)
        clear_btn.pack(side='right')

        body = tk.Frame(f, bg=C['bg'])
        body.pack(fill='both', expand=True, padx=10, pady=6)

        # import buttons
        ib = tk.Frame(body, bg=C['bg'])
        ib.pack(fill='x', pady=(0,6))
        self._btn(ib, '📁  Import CSV',    self._import_csv).pack(
            side='left', fill='x', expand=True, padx=(0,3))
        self._btn(ib, '📊  Google Sheet',  self._open_sheets_dlg).pack(
            side='left', fill='x', expand=True)

        # auto-dial controls
        ad = tk.Frame(body, bg=C['bg2'], bd=1, relief='solid')
        ad.pack(fill='x', pady=(0,6))
        ad_inner = tk.Frame(ad, bg=C['bg2'])
        ad_inner.pack(fill='x', padx=8, pady=6)

        r1 = tk.Frame(ad_inner, bg=C['bg2'])
        r1.pack(fill='x', pady=(0,4))
        tk.Label(r1, text='Auto-dial next call', bg=C['bg2'],
                 fg=C['txt1'], font=('Segoe UI',9)).pack(side='left')
        self.ad_chk = ttk.Checkbutton(r1, variable=self.auto_dial,
                                       style='TCheckbutton')
        self.ad_chk.pack(side='right')

        r2 = tk.Frame(ad_inner, bg=C['bg2'])
        r2.pack(fill='x')
        tk.Label(r2, text='Delay (seconds)', bg=C['bg2'],
                 fg=C['txt1'], font=('Segoe UI',9)).pack(side='left')
        self.delay_var = tk.StringVar(value=self.cfg['dial_delay'])
        ttk.Entry(r2, textvariable=self.delay_var, width=5).pack(side='right')

        # queue controls
        qc = tk.Frame(body, bg=C['bg'])
        qc.pack(fill='x', pady=(0,6))
        self.start_btn = ttk.Button(qc, text='▶  Start Queue',
                                     style='Success.TButton',
                                     command=self._start_queue)
        self.start_btn.pack(side='left', fill='x', expand=True, padx=(0,3))
        self.pause_btn = self._btn(qc, '⏸', self._pause_queue,
                                    state='disabled', width=3)
        self.pause_btn.pack(side='left', padx=(0,3))
        self._btn(qc, '⏭', self._skip_current, width=3).pack(side='left')

        # stats
        sf = tk.Frame(body, bg=C['bg'])
        sf.pack(fill='x', pady=(0,4))
        for lbl, key, col in [
            ('Total', 'stat_total', C['txt0']),
            ('Done',  'stat_done',  C['green']),
            ('Left',  'stat_left',  C['txt2']),
        ]:
            box = tk.Frame(sf, bg=C['bg2'], bd=1, relief='solid')
            box.pack(side='left', fill='x', expand=True, padx=2)
            v = tk.Label(box, text='0', bg=C['bg2'], fg=col,
                         font=('Segoe UI', 14, 'bold'))
            v.pack()
            tk.Label(box, text=lbl, bg=C['bg2'], fg=C['txt2'],
                     font=('Segoe UI', 7)).pack()
            setattr(self, key, v)

        # progress bar (canvas)
        self.q_canvas = tk.Canvas(body, height=4, bg=C['bg2'],
                                   highlightthickness=0)
        self.q_canvas.pack(fill='x', pady=(2,4))
        self.q_bar_fill = self.q_canvas.create_rectangle(
            0, 0, 0, 4, fill=C['blue'], outline='')
        self.q_canvas.bind('<Configure>', self._redraw_qbar)

        # search
        sv = tk.StringVar()
        sv.trace_add('write', lambda *_: self._filter(sv.get()))
        self.search_var = sv
        se = ttk.Entry(body, textvariable=sv)
        se.insert(0, '🔍  Search contacts…')
        se.bind('<FocusIn>',  lambda e: se.delete(0,'end') if se.get().startswith('🔍') else None)
        se.pack(fill='x', pady=(0,4))

        # contact list (canvas + scrollbar)
        cf = tk.Frame(body, bg=C['bg'])
        cf.pack(fill='both', expand=True)
        vsb = ttk.Scrollbar(cf, orient='vertical')
        vsb.pack(side='right', fill='y')
        self.clist_canvas = tk.Canvas(cf, bg=C['bg'], yscrollcommand=vsb.set,
                                       highlightthickness=0)
        self.clist_canvas.pack(side='left', fill='both', expand=True)
        vsb.config(command=self.clist_canvas.yview)
        self.clist_inner = tk.Frame(self.clist_canvas, bg=C['bg'])
        self._clist_window = self.clist_canvas.create_window(
            (0,0), window=self.clist_inner, anchor='nw')
        self.clist_inner.bind('<Configure>', self._on_clist_configure)
        self.clist_canvas.bind('<Configure>', lambda e:
            self.clist_canvas.itemconfig(self._clist_window, width=e.width))
        self.clist_canvas.bind('<MouseWheel>',
            lambda e: self.clist_canvas.yview_scroll(-int(e.delta/60),'units'))

        self._render_contacts()
        return f

    def _on_clist_configure(self, _e=None):
        self.clist_canvas.configure(
            scrollregion=self.clist_canvas.bbox('all'))

    # ──────────────────────────────────────────────────────────────────
    #  CENTER PANEL — ACTIVE CALL
    # ──────────────────────────────────────────────────────────────────
    def _center_panel(self, parent):
        f = tk.Frame(parent, bg=C['bg'])
        self._panel_header(f, "📞  Active Call")

        # ── call display ─────────────────────────────────────────────
        disp = tk.Frame(f, bg=C['bg1'])
        disp.pack(fill='x')
        tk.Frame(disp, bg=C['border'], height=1).pack(fill='x', side='bottom')

        self.status_lbl = tk.Label(disp, text='IDLE',
                                    bg=C['bg1'], fg=C['txt2'],
                                    font=('Segoe UI', 8, 'bold'))
        self.status_lbl.pack(pady=(16,4))
        self.call_name = tk.Label(disp, text='—',
                                   bg=C['bg1'], fg=C['txt0'],
                                   font=('Segoe UI', 22, 'bold'))
        self.call_name.pack()
        self.call_num = tk.Label(disp, text='No active call',
                                  bg=C['bg1'], fg=C['txt1'],
                                  font=('Consolas', 11))
        self.call_num.pack(pady=(2,4))
        self.timer_lbl = tk.Label(disp, text='Ready to dial',
                                   bg=C['bg1'], fg=C['txt2'],
                                   font=('Consolas', 18))
        self.timer_lbl.pack(pady=(0,14))
        self.conf_badge = tk.Label(disp, text='🤝  3-Way Conference Active',
                                    bg=C['bg1'], fg=C['purple'],
                                    font=('Segoe UI', 9))
        # not packed until conference starts

        # ── body ─────────────────────────────────────────────────────
        body = tk.Frame(f, bg=C['bg'])
        body.pack(fill='both', expand=True, padx=20, pady=10)

        # controls row 1: mute hold speaker dtmf
        r1 = tk.Frame(body, bg=C['bg'])
        r1.pack(pady=(4,6))
        ctrl_btns = [
            ('🎤  Mute',   self._toggle_mute,    'mute_btn'),
            ('⏸  Hold',   self._toggle_hold,    'hold_btn'),
            ('🔊  Speaker', self._toggle_speaker, 'spk_btn'),
            ('#  Keypad',  self._toggle_dialpad,  'dp_btn'),
        ]
        for label, cmd, attr in ctrl_btns:
            b = self._btn(r1, label, cmd)
            b.pack(side='left', padx=3)
            setattr(self, attr, b)

        # dialpad (hidden by default)
        self.dialpad_frame = tk.Frame(body, bg=C['bg'])
        self._build_dialpad(self.dialpad_frame)

        # manual dial
        mr = tk.Frame(body, bg=C['bg'])
        mr.pack(pady=4)
        self.manual_var = tk.StringVar()
        me = ttk.Entry(mr, textvariable=self.manual_var,
                       width=22, font=('Consolas', 12),
                       justify='center')
        me.pack(side='left', padx=(0,5))
        me.bind('<Return>', lambda _: self._dial_manual())
        self._btn(mr, '📞  Call', self._dial_manual,
                  style='Primary.TButton').pack(side='left')

        # big action buttons
        ab = tk.Frame(body, bg=C['bg'])
        ab.pack(pady=10)
        self.next_btn = ttk.Button(ab, text='▶  Next Contact',
                                    style='Success.TButton',
                                    command=self._dial_next)
        self.next_btn.pack(side='left', ipadx=10, ipady=6, padx=5)
        self.end_btn  = ttk.Button(ab, text='📵  End Call',
                                    style='Danger.TButton',
                                    command=self._end_call,
                                    state='disabled')
        self.end_btn.pack(side='left', ipadx=10, ipady=6, padx=5)

        # auto-dial countdown
        self.cd_frame = tk.Frame(body, bg=C['bg2'], bd=1, relief='solid')
        self.cd_lbl   = tk.Label(self.cd_frame,
                                  bg=C['bg2'], fg=C['blue'],
                                  font=('Segoe UI', 9))
        self.cd_lbl.pack(side='left', padx=8, pady=4)
        self._btn(self.cd_frame, '✕', self._cancel_countdown,
                  small=True).pack(side='right', padx=4)
        # cd_frame shown/hidden dynamically

        # attended-transfer banner
        self.att_frame = tk.Frame(body, bg='#1f2d4a', bd=1, relief='solid')
        tk.Label(self.att_frame,
                 text='🔀  Attended transfer — speaking with target',
                 bg='#1f2d4a', fg=C['cyan'],
                 font=('Segoe UI', 9)).pack(side='left', padx=8, pady=4)
        self._btn(self.att_frame, '✓ Complete',
                  self.sip.attended_transfer_complete,
                  style='Primary.TButton', small=True).pack(side='left', padx=3)
        self._btn(self.att_frame, '✕ Cancel',
                  self.sip.attended_transfer_cancel,
                  small=True).pack(side='left', padx=3)
        # att_frame hidden until attended transfer starts

        self._update_ctrl_state()
        return f

    def _build_dialpad(self, parent):
        keys = [
            ('1',''),    ('2','ABC'),  ('3','DEF'),
            ('4','GHI'), ('5','JKL'),  ('6','MNO'),
            ('7','PQRS'),('8','TUV'),  ('9','WXYZ'),
            ('*',''),    ('0','+'),    ('#',''),
        ]
        for i, (main, sub) in enumerate(keys):
            r, c = divmod(i, 3)
            txt = f"{main}\n{sub}" if sub else main
            b = tk.Button(parent, text=txt, bg=C['bg2'], fg=C['txt0'],
                          activebackground=C['bg3'], activeforeground=C['txt0'],
                          font=('Segoe UI', 11, 'bold'), width=4, height=2,
                          bd=0, relief='flat',
                          command=lambda k=main: self._dp_press(k))
            b.grid(row=r, column=c, padx=3, pady=3)

    # ──────────────────────────────────────────────────────────────────
    #  RIGHT PANEL — DETAILS & NOTES
    # ──────────────────────────────────────────────────────────────────
    def _right_panel(self, parent):
        f = tk.Frame(parent, bg=C['bg'])
        self._panel_header(f, "👤  Contact Details & Notes")

        body = tk.Frame(f, bg=C['bg'])
        body.pack(fill='both', expand=True, padx=10, pady=6)

        # detail card
        dc = tk.Frame(body, bg=C['bg2'], bd=1, relief='solid')
        dc.pack(fill='x', pady=(0,8))
        self._detail_labels = {}
        for key, lbl in [
            ('name',    'Name'),
            ('phone',   'Phone'),
            ('email',   'Email'),
            ('company', 'Company'),
        ]:
            row = tk.Frame(dc, bg=C['bg2'])
            row.pack(fill='x', padx=10, pady=2)
            tk.Label(row, text=lbl, bg=C['bg2'], fg=C['txt2'],
                     font=('Segoe UI', 8), width=8, anchor='w').pack(side='left')
            v = tk.Label(row, text='—', bg=C['bg2'], fg=C['txt1'],
                         font=('Segoe UI', 9), anchor='w', wraplength=160)
            v.pack(side='left', fill='x', expand=True)
            self._detail_labels[key] = v
        self._extra_frame = tk.Frame(dc, bg=C['bg2'])
        self._extra_frame.pack(fill='x', padx=10, pady=(0,6))

        # outcome
        self._sec_label(body, 'Call Outcome')
        og = tk.Frame(body, bg=C['bg'])
        og.pack(fill='x', pady=(0,8))
        og.columnconfigure(0, weight=1)
        og.columnconfigure(1, weight=1)
        outcomes = [
            ('✅  Answered',  'answered',  C['green']),
            ('📵  No Answer', 'noanswer',  C['txt2']),
            ('🔴  Busy',      'busy',      C['yellow']),
            ('📅  Callback',  'callback',  C['purple']),
            ('🚫  DNC',       'dnc',       C['red']),
            ('⚠  Failed',    'failed',    C['orange']),
        ]
        self._out_btns = {}
        for i, (label, key, _col) in enumerate(outcomes):
            r, c = divmod(i, 2)
            b = tk.Button(og, text=label, bg=C['bg2'], fg=C['txt1'],
                          activebackground=C['bg3'],
                          font=('Segoe UI', 8, 'bold'),
                          bd=1, relief='solid',
                          command=lambda k=key: self._set_outcome(k))
            b.grid(row=r, column=c, sticky='ew', padx=2, pady=2)
            self._out_btns[key] = (b, _col)

        self._divider(body)

        # previous notes
        self._sec_label(body, 'Previous Notes')
        nf = tk.Frame(body, bg=C['bg'])
        nf.pack(fill='both', expand=False, pady=(0,6))
        notes_vsb = ttk.Scrollbar(nf)
        notes_vsb.pack(side='right', fill='y')
        self.notes_text = tk.Text(nf, height=7, bg=C['bg2'], fg=C['txt1'],
                                   font=('Segoe UI', 8), bd=0, relief='flat',
                                   yscrollcommand=notes_vsb.set,
                                   state='disabled', wrap='word',
                                   insertbackground=C['txt0'],
                                   selectbackground=C['bg3'])
        self.notes_text.pack(side='left', fill='both', expand=True)
        notes_vsb.config(command=self.notes_text.yview)
        self.notes_text.tag_config('meta',   foreground=C['txt2'], font=('Segoe UI',7))
        self.notes_text.tag_config('out_ok', foreground=C['green'])
        self.notes_text.tag_config('out_warn',foreground=C['yellow'])
        self.notes_text.tag_config('out_bad', foreground=C['red'])

        self._divider(body)

        # add note
        self._sec_label(body, 'Add Note')
        self.note_var = tk.StringVar()
        note_entry = tk.Text(body, height=3, bg=C['bg2'], fg=C['txt0'],
                              font=('Segoe UI', 9), bd=1, relief='solid',
                              insertbackground=C['txt0'],
                              selectbackground=C['bg3'],
                              wrap='word')
        note_entry.pack(fill='x', pady=(0,5))
        self.note_input = note_entry
        ttk.Button(body, text='💾  Save Note', style='Primary.TButton',
                   command=self._save_note).pack(fill='x')

        return f

    # ──────────────────────────────────────────────────────────────────
    #  UI HELPERS
    # ──────────────────────────────────────────────────────────────────
    def _panel_header(self, parent, title):
        hdr = tk.Frame(parent, bg=C['bg1'], height=38)
        hdr.pack(fill='x')
        hdr.pack_propagate(False)
        tk.Label(hdr, text=title, bg=C['bg1'], fg=C['txt2'],
                 font=('Segoe UI', 8, 'bold')).pack(side='left', padx=12, pady=8)
        tk.Frame(parent, bg=C['border'], height=1).pack(fill='x')
        return hdr

    def _btn(self, parent, text, cmd, style='TButton',
             state='normal', small=False, width=None):
        kw = dict(command=cmd, state=state, style=style)
        if width: kw['width'] = width
        if small: style = 'TButton'
        b = ttk.Button(parent, text=text, **kw)
        return b

    def _sec_label(self, parent, text):
        tk.Label(parent, text=text.upper(),
                 bg=C['bg'], fg=C['txt2'],
                 font=('Segoe UI', 7, 'bold')).pack(anchor='w', pady=(4,3))

    def _divider(self, parent):
        tk.Frame(parent, bg=C['border'], height=1).pack(fill='x', pady=5)

    # ──────────────────────────────────────────────────────────────────
    #  CONTACT LIST RENDERING
    # ──────────────────────────────────────────────────────────────────
    def _render_contacts(self):
        for w in self.clist_inner.winfo_children():
            w.destroy()

        if not self.contacts:
            tk.Label(self.clist_inner,
                     text="Import a CSV or Google Sheet\nto load contacts",
                     bg=C['bg'], fg=C['txt2'],
                     font=('Segoe UI', 9), justify='center').pack(pady=30)
            self._update_queue_stats()
            return

        src = self.filtered if self.filtered is not None else self.contacts
        for contact in src:
            idx = self.contacts.index(contact)
            is_active = (idx == self.cur_idx)

            row_bg = C['bg2'] if is_active else C['bg']
            row = tk.Frame(self.clist_inner, bg=row_bg,
                           cursor='hand2')
            row.pack(fill='x', pady=1, padx=2)

            # avatar letter
            letter = (contact.get('name') or contact.get('phone', '?'))[0].upper()
            col = string_colour(contact.get('name') or contact.get('phone', ''))
            av = tk.Label(row, text=letter, bg=col, fg='white',
                          font=('Segoe UI', 10, 'bold'), width=2)
            av.pack(side='left', padx=(6,6), pady=4)

            info = tk.Frame(row, bg=row_bg)
            info.pack(side='left', fill='x', expand=True)
            tk.Label(info, text=contact.get('name') or contact.get('phone',''),
                     bg=row_bg, fg=C['txt0'],
                     font=('Segoe UI', 9, 'bold'), anchor='w').pack(anchor='w')
            tk.Label(info, text=contact.get('phone',''),
                     bg=row_bg, fg=C['txt2'],
                     font=('Consolas', 8), anchor='w').pack(anchor='w')

            # status badge
            status = contact.get('status', 'pending')
            s_col  = self._status_colour(status)
            tk.Label(row, text=f'  {status}  ', bg=row_bg,
                     fg=s_col, font=('Segoe UI', 7, 'bold')).pack(side='right',padx=4)

            # row number
            tk.Label(row, text=f"#{contact.get('rownum', idx+1)}",
                     bg=row_bg, fg=C['txt2'],
                     font=('Segoe UI', 7)).pack(side='right', padx=2)

            for w in (row, av, info):
                w.bind('<Button-1>', lambda e, i=idx: self._select_contact(i))

        self._update_queue_stats()
        self._on_clist_configure()

    def _status_colour(self, status):
        return {
            'pending':   C['txt2'],
            'calling':   C['blue'],
            'active':    C['green'],
            'completed': C['green'],
            'answered':  C['green'],
            'noanswer':  C['txt2'],
            'busy':      C['yellow'],
            'callback':  C['purple'],
            'dnc':       C['red'],
            'failed':    C['red'],
            'skipped':   C['txt2'],
        }.get(status, C['txt2'])

    def _update_queue_stats(self):
        total  = len(self.contacts)
        done   = sum(1 for c in self.contacts if c.get('status','') in STATUS_DONE)
        left   = sum(1 for c in self.contacts if c.get('status','pending') == 'pending')
        self.stat_total.config(text=str(total))
        self.stat_done.config( text=str(done))
        self.stat_left.config( text=str(left))
        # progress bar
        pct = done / total if total else 0
        canvas = self.q_canvas
        canvas.update_idletasks()
        w = canvas.winfo_width()
        canvas.coords(self.q_bar_fill, 0, 0, int(w * pct), 4)

    def _redraw_qbar(self, _e=None):
        self._update_queue_stats()

    def _filter(self, query):
        q = query.lower().strip()
        if not q or q.startswith('🔍'):
            self.filtered = self.contacts[:]
        else:
            self.filtered = [
                c for c in self.contacts
                if q in (c.get('name','') or '').lower()
                or q in (c.get('phone','') or '')
                or q in (c.get('company','') or '').lower()
            ]
        self._render_contacts()

    def _select_contact(self, idx):
        self.cur_idx = idx
        self._update_details(self.contacts[idx])
        self._render_contacts()

    def _update_details(self, contact):
        for key in ('name','phone','email','company'):
            v = contact.get(key) or ''
            lbl = self._detail_labels[key]
            lbl.config(text=v or '—', fg=C['txt0'] if v else C['txt2'])

        # extra fields
        for w in self._extra_frame.winfo_children():
            w.destroy()
        for k, v in list((contact.get('extra') or {}).items())[:5]:
            row = tk.Frame(self._extra_frame, bg=C['bg2'])
            row.pack(fill='x', pady=1)
            tk.Label(row, text=k, bg=C['bg2'], fg=C['txt2'],
                     font=('Segoe UI', 8), width=8, anchor='w').pack(side='left')
            tk.Label(row, text=str(v), bg=C['bg2'], fg=C['txt1'],
                     font=('Segoe UI', 9), anchor='w',
                     wraplength=160).pack(side='left')

        # outcome buttons reset
        for b, _ in self._out_btns.values():
            b.config(bg=C['bg2'], fg=C['txt1'])
        self.outcome = None

        # load notes
        key = contact_key(contact)
        self._render_notes(key)
        self.note_input.delete('1.0', 'end')

    def _render_notes(self, key):
        self.notes_text.config(state='normal')
        self.notes_text.delete('1.0', 'end')
        rows = self.notes_db.get(key)
        if not rows:
            self.notes_text.insert('end', 'No notes yet', 'meta')
        else:
            for (dt, outcome, note) in rows:
                out_tag = ('out_ok' if outcome in ('answered','completed')
                           else 'out_bad' if outcome in ('dnc','failed')
                           else 'out_warn')
                self.notes_text.insert('end', f"{dt}  ", 'meta')
                self.notes_text.insert('end', f"[{outcome}]\n", out_tag)
                if note:
                    self.notes_text.insert('end', f"{note}\n", '')
                self.notes_text.insert('end', '─'*30 + '\n', 'meta')
        self.notes_text.config(state='disabled')
        self.notes_text.see('1.0')

    # ──────────────────────────────────────────────────────────────────
    #  CALL CONTROLS
    # ──────────────────────────────────────────────────────────────────
    def _end_call(self):
        self.sip.hangup()
        self._on_call_ended()

    def _toggle_mute(self):
        self._mute_state = self.sip.toggle_mute()
        self.mute_btn.config(
            text=('🔇  Unmute' if self._mute_state else '🎤  Mute'))

    def _toggle_hold(self):
        if self._hold_state:
            self.sip.unhold()
            self._hold_state = False
            self.hold_btn.config(text='⏸  Hold')
        else:
            self.sip.hold()
            self._hold_state = True
            self.hold_btn.config(text='▶  Unhold')

    def _toggle_speaker(self):
        # Speaker boost: handled by OS — just a UI toggle here
        self.spk_btn.config(
            fg=(C['blue'] if self.spk_btn.cget('fg') == C['txt0']
                else C['txt0']))

    def _toggle_dialpad(self):
        if self.dialpad_frame.winfo_ismapped():
            self.dialpad_frame.pack_forget()
        else:
            self.dialpad_frame.pack(pady=4)

    def _dp_press(self, key):
        self.sip.dtmf(key)
        cur = self.manual_var.get()
        self.manual_var.set(cur + key)

    def _dial_manual(self):
        num = self.manual_var.get().strip()
        if not num:
            self._toast('Enter a phone number first', 'warn')
            return
        self.cur_idx = -1
        self.outcome = None
        self._do_dial(num, num)

    def _dial_next(self):
        self._cancel_countdown()
        idx = self._next_pending(self.cur_idx + 1)
        if idx == -1:
            self._toast('No more pending contacts in queue', 'info')
            return
        self._dial_contact(idx)

    def _dial_contact(self, idx):
        c = self.contacts[idx]
        if not c.get('phone'):
            self._toast(f'Contact #{idx+1} has no phone number — skipped', 'warn')
            self._mark_status(idx, 'skipped')
            if self.queue_running:
                self._schedule_next()
            return
        self.cur_idx = idx
        self.outcome = None
        self._mark_status(idx, 'calling')
        self._update_details(c)
        self._render_contacts()
        self._scroll_to(idx)
        self._do_dial(c['phone'], c.get('name',''))

    def _do_dial(self, number, display_name):
        try:
            self.sip.dial(number)
            self._set_call_display(display_name or number, number, 'CONNECTING…', C['blue'])
            self._update_ctrl_state(in_call=True)
        except Exception as e:
            self._toast(str(e), 'error')
            if self.cur_idx >= 0:
                self._mark_status(self.cur_idx, 'failed')
            if self.queue_running:
                self._schedule_next()

    def _skip_current(self):
        if self.cur_idx >= 0:
            self._mark_status(self.cur_idx, 'skipped')
        self._cancel_countdown()
        if self.sip.state in ('active','calling','ringing'):
            self._end_call()
        elif self.queue_running:
            self._schedule_next()

    # ──────────────────────────────────────────────────────────────────
    #  QUEUE
    # ──────────────────────────────────────────────────────────────────
    def _start_queue(self):
        if not self.contacts:
            self._toast('Import contacts first', 'warn'); return
        if self.sip.state not in ('registered','idle'):
            self._toast('Connect to SIP server first', 'error'); return
        self.queue_running = True
        self.start_btn.config(state='disabled')
        self.pause_btn.config(state='normal')
        idx = self._next_pending(0)
        if idx == -1:
            self._toast('No pending contacts', 'warn')
            self.queue_running = False
            return
        self._dial_contact(idx)

    def _pause_queue(self):
        self.queue_running = False
        self._cancel_countdown()
        self.start_btn.config(state='normal')
        self.pause_btn.config(state='disabled')
        self._toast('Queue paused', 'info')

    def _clear_queue(self):
        if not self.contacts: return
        if not messagebox.askyesno('Clear Queue',
                                    'Remove all contacts from the queue?'):
            return
        self.contacts  = []
        self.filtered  = []
        self.cur_idx   = -1
        self._render_contacts()

    def _next_pending(self, from_idx=0):
        for i in range(from_idx, len(self.contacts)):
            if self.contacts[i].get('status', 'pending') == 'pending':
                return i
        return -1

    def _mark_status(self, idx, status):
        if 0 <= idx < len(self.contacts):
            self.contacts[idx]['status'] = status
        self._update_queue_stats()

    def _schedule_next(self):
        self._cancel_countdown()
        idx = self._next_pending(self.cur_idx + 1)
        if idx == -1:
            self._toast('🎉  Queue complete — all contacts dialed!', 'info')
            self.queue_running = False
            self.start_btn.config(state='normal')
            self.pause_btn.config(state='disabled')
            return

        try:
            delay = max(1, int(self.delay_var.get()))
        except ValueError:
            delay = 5

        self._cd_remain = delay
        self.cd_frame.pack(fill='x', pady=4)
        self._countdown_tick(idx, delay)

    def _countdown_tick(self, next_idx, total):
        self.cd_lbl.config(
            text=f'Next call in  {self._cd_remain}s  ({self._cd_remain}/{total})')
        if self._cd_remain <= 0:
            self.cd_frame.pack_forget()
            if self.queue_running and self.sip.state in ('registered','idle'):
                self._dial_contact(next_idx)
            return
        self._cd_remain -= 1
        self._cd_job = self.after(1000, self._countdown_tick, next_idx, total)

    def _cancel_countdown(self):
        if self._cd_job:
            self.after_cancel(self._cd_job)
            self._cd_job = None
        self.cd_frame.pack_forget()

    def _scroll_to(self, idx):
        self.clist_canvas.update_idletasks()
        items = self.clist_inner.winfo_children()
        if idx < len(items):
            y = items[idx].winfo_y()
            h = self.clist_inner.winfo_height()
            self.clist_canvas.yview_moveto(y / max(h, 1))

    # ──────────────────────────────────────────────────────────────────
    #  OUTCOMES & NOTES
    # ──────────────────────────────────────────────────────────────────
    def _set_outcome(self, key):
        self.outcome = key
        for k, (b, col) in self._out_btns.items():
            if k == key:
                b.config(bg=C['bg3'], fg=col)
            else:
                b.config(bg=C['bg2'], fg=C['txt1'])
        # auto-update contact status
        if self.cur_idx >= 0:
            self._mark_status(self.cur_idx, key)
            self._render_contacts()

    def _save_note(self):
        note_text = self.note_input.get('1.0', 'end').strip()
        outcome   = self.outcome or 'note'
        if not note_text and not self.outcome:
            self._toast('Enter a note or select an outcome first', 'warn')
            return
        contact = (self.contacts[self.cur_idx]
                   if 0 <= self.cur_idx < len(self.contacts) else None)
        key = contact_key(contact) if contact else 'manual'
        self.notes_db.add(key, note_text, outcome)
        self.note_input.delete('1.0', 'end')
        self._render_notes(key)
        self._toast('Note saved ✓', 'info')

    # ──────────────────────────────────────────────────────────────────
    #  SIP EVENT → UI (via queue)
    # ──────────────────────────────────────────────────────────────────
    def _poll_ui_queue(self):
        try:
            while True:
                event, data = self._ui_q.get_nowait()
                self._handle_sip_event(event, data)
        except queue.Empty:
            pass
        self.after(80, self._poll_ui_queue)

    def _handle_sip_event(self, event, data):
        if event == 'registered':
            self.sip_dot.config(fg=C['green'])
            self.sip_lbl.config(text=f'Registered: {data}')
            self._toast(f'SIP registered ✓', 'info')

        elif event == 'disconnected':
            self.sip_dot.config(fg=C['txt2'])
            self.sip_lbl.config(text='Not Connected')

        elif event == 'calling':
            self._set_call_display('…', str(data), 'CONNECTING…', C['blue'])

        elif event == 'ringing':
            self.status_lbl.config(text='RINGING…', fg=C['yellow'])
            if self.cur_idx >= 0:
                self._mark_status(self.cur_idx, 'calling')

        elif event == 'answered':
            if self.cur_idx >= 0:
                c = self.contacts[self.cur_idx]
                self._set_call_display(
                    c.get('name', c.get('phone', '—')),
                    c.get('phone',''), 'IN CALL', C['green'])
            self.status_lbl.config(text='IN CALL', fg=C['green'])
            self._start_timer()
            self._update_ctrl_state(in_call=True)

        elif event == 'ended':
            self._on_call_ended()

        elif event == 'conference':
            self.conf_badge.pack(pady=(0,8))
            self.status_lbl.config(text='CONFERENCE', fg=C['purple'])

        elif event == 'conf_answered':
            self.conf_badge.pack(pady=(0,8))
            self.status_lbl.config(text='CONFERENCE', fg=C['purple'])

        elif event == 'conf_ended':
            self.conf_badge.pack_forget()
            self.status_lbl.config(text='IN CALL', fg=C['green'])

        elif event == 'attended_answered':
            self.att_frame.pack(fill='x', pady=4)

        elif event == 'attended_ended':
            self.att_frame.pack_forget()

        elif event == 'incoming':
            self._handle_incoming(data)

        elif event == 'error':
            self._toast(str(data), 'error')

    def _handle_incoming(self, call):
        remote = getattr(call, '_uri', '') or '(unknown)'
        ans = messagebox.askyesno(
            'Incoming Call',
            f'Incoming call from:\n{remote}\n\nAnswer?')
        if ans:
            self.sip.answer(call)
        else:
            try: call.deny()
            except Exception: pass

    def _on_call_ended(self):
        self._stop_timer()
        self._hold_state = False
        self._mute_state = False
        self.hold_btn.config(text='⏸  Hold')
        self.mute_btn.config(text='🎤  Mute')
        self.conf_badge.pack_forget()
        self.att_frame.pack_forget()

        status = self.outcome or 'completed'
        if self.cur_idx >= 0:
            self._mark_status(self.cur_idx, status)
        self._render_contacts()

        self._set_call_display('—', 'No active call', 'IDLE', C['txt2'])
        self.timer_lbl.config(text='Ready to dial', fg=C['txt2'])
        self._update_ctrl_state(in_call=False)

        if self.queue_running and self.auto_dial.get():
            self._schedule_next()

    # ──────────────────────────────────────────────────────────────────
    #  CALL TIMER
    # ──────────────────────────────────────────────────────────────────
    def _start_timer(self):
        self._stop_timer()
        self._call_secs = 0
        self._tick_timer()

    def _tick_timer(self):
        h, r = divmod(self._call_secs, 3600)
        m, s = divmod(r, 60)
        t = (f'{h}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}')
        self.timer_lbl.config(text=t, fg=C['txt0'])
        self._call_secs += 1
        self._timer_job = self.after(1000, self._tick_timer)

    def _stop_timer(self):
        if self._timer_job:
            self.after_cancel(self._timer_job)
            self._timer_job = None

    # ──────────────────────────────────────────────────────────────────
    #  CTRL STATE
    # ──────────────────────────────────────────────────────────────────
    def _update_ctrl_state(self, in_call=False):
        s = 'normal' if in_call else 'disabled'
        self.end_btn.config(  state=s)
        self.mute_btn.config( state=s)
        self.hold_btn.config( state=s)
        self.conf_btn.config( state=s)
        self.xfer_btn.config( state=s)

    def _set_call_display(self, name, number, status_text, status_col):
        self.call_name.config(text=name)
        self.call_num.config(text=number)
        self.status_lbl.config(text=status_text, fg=status_col)

    # ──────────────────────────────────────────────────────────────────
    #  CSV IMPORT
    # ──────────────────────────────────────────────────────────────────
    def _import_csv(self):
        path = filedialog.askopenfilename(
            title='Select CSV file',
            filetypes=[('CSV files','*.csv'), ('Text files','*.txt'),
                       ('All files','*.*')])
        if not path: return
        try:
            with open(path, newline='', encoding='utf-8-sig') as f:
                rows = list(csv.DictReader(f))
            if not rows:
                self._toast('CSV file is empty', 'error'); return
            self._column_mapper(rows, list(rows[0].keys()))
        except Exception as e:
            self._toast(f'CSV error: {e}', 'error')

    def _open_sheets_dlg(self):
        dlg = tk.Toplevel(self)
        dlg.title('Import Google Sheet')
        dlg.configure(bg=C['bg'])
        dlg.geometry('460x220')
        dlg.resizable(False, False)

        tk.Label(dlg, text='Google Sheet URL', bg=C['bg'],
                 fg=C['txt1'], font=('Segoe UI',9)).pack(padx=20,pady=(18,4),anchor='w')
        url_var = tk.StringVar()
        ttk.Entry(dlg, textvariable=url_var, width=55).pack(padx=20, fill='x')
        tk.Label(dlg, text='⚠  Sheet must be publicly shared ("Anyone with link can view")',
                 bg=C['bg'], fg=C['yellow'],
                 font=('Segoe UI',8), wraplength=400).pack(padx=20,pady=4)
        tk.Label(dlg, text='Sheet tab (GID) — leave 0 for first tab',
                 bg=C['bg'], fg=C['txt1'],
                 font=('Segoe UI',9)).pack(padx=20,pady=(4,2),anchor='w')
        gid_var = tk.StringVar(value='0')
        ttk.Entry(dlg, textvariable=gid_var, width=12).pack(padx=20, anchor='w')

        def _do_import():
            url = url_var.get().strip()
            gid = gid_var.get().strip() or '0'
            m = re.search(r'/spreadsheets/d/([a-zA-Z0-9_-]+)', url)
            if not m:
                self._toast('Invalid Google Sheet URL', 'error'); return
            sheet_id = m.group(1)
            fetch_url = (f'https://docs.google.com/spreadsheets/d/{sheet_id}'
                         f'/gviz/tq?tqx=out:csv&gid={gid}')
            dlg.destroy()
            self._toast('Fetching Google Sheet…', 'info')
            threading.Thread(target=self._fetch_sheet,
                             args=(fetch_url,), daemon=True).start()

        bf = tk.Frame(dlg, bg=C['bg'])
        bf.pack(pady=12)
        self._btn(bf, 'Cancel', dlg.destroy).pack(side='left', padx=5)
        ttk.Button(bf, text='📥  Import', style='Primary.TButton',
                   command=_do_import).pack(side='left', padx=5)

    def _fetch_sheet(self, url):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as r:
                text = r.read().decode('utf-8')
            rows = list(csv.DictReader(io.StringIO(text)))
            if not rows:
                self.after(0, lambda: self._toast('Sheet appears empty', 'error'))
                return
            self.after(0, lambda: self._column_mapper(rows, list(rows[0].keys())))
        except Exception as e:
            self.after(0, lambda: self._toast(
                f'Could not fetch sheet.\nMake sure it is public.\n{e}', 'error'))

    def _column_mapper(self, rows, headers):
        dlg = tk.Toplevel(self)
        dlg.title('Map CSV Columns')
        dlg.configure(bg=C['bg'])
        dlg.geometry('480x380')

        tk.Label(dlg, text=f'{len(rows)} rows · {len(headers)} columns detected',
                 bg=C['bg'], fg=C['txt1'],
                 font=('Segoe UI',9)).pack(padx=20, pady=(14,6), anchor='w')

        # preview
        prev_f = tk.Frame(dlg, bg=C['bg2'], bd=1, relief='solid')
        prev_f.pack(fill='x', padx=20, pady=(0,10))
        prev_t = tk.Text(prev_f, height=3, bg=C['bg2'], fg=C['txt2'],
                         font=('Consolas',8), bd=0, state='normal')
        prev_t.pack(fill='x', padx=6, pady=4)
        preview_line = ' | '.join(headers[:8])
        prev_t.insert('end', preview_line + '\n')
        if rows:
            first = ' | '.join(str(rows[0].get(h,'')) for h in headers[:8])
            prev_t.insert('end', first)
        prev_t.config(state='disabled')

        grid = tk.Frame(dlg, bg=C['bg'])
        grid.pack(padx=20, fill='x')
        fields  = [('phone','Phone Number *'),('name','Full Name'),
                   ('email','Email'),('company','Company')]
        selects = {}
        auto_patterns = {
            'phone':   ('phone','mobile','cell','tel','number','num','contact'),
            'name':    ('name','full','contact','person','client','customer'),
            'email':   ('email','mail'),
            'company': ('company','org','business','firm','employer'),
        }
        for i, (fkey, flbl) in enumerate(fields):
            r, c = divmod(i, 2)
            tk.Label(grid, text=flbl, bg=C['bg'], fg=C['txt1'],
                     font=('Segoe UI',8)).grid(row=r*2, column=c, sticky='w', padx=5)
            var = tk.StringVar()
            # auto-match
            for h in headers:
                hl = h.lower().replace(' ','').replace('_','')
                if any(p in hl for p in auto_patterns.get(fkey,())):
                    var.set(h); break
            cb = ttk.Combobox(grid, textvariable=var,
                              values=['(skip)'] + headers, width=20, state='readonly')
            cb.grid(row=r*2+1, column=c, sticky='w', padx=5, pady=(0,8))
            selects[fkey] = var

        mode_var = tk.StringVar(value='replace')
        mf = tk.Frame(dlg, bg=C['bg'])
        mf.pack(padx=20, fill='x')
        tk.Label(mf, text='Import mode:', bg=C['bg'],
                 fg=C['txt1'], font=('Segoe UI',8)).pack(side='left')
        ttk.Combobox(mf, textvariable=mode_var,
                     values=['replace','append'], width=10,
                     state='readonly').pack(side='left', padx=6)

        def _confirm():
            phone_col = selects['phone'].get()
            if not phone_col or phone_col == '(skip)':
                self._toast('You must select the Phone Number column', 'error')
                return
            name_col    = selects['name'].get()
            email_col   = selects['email'].get()
            company_col = selects['company'].get()

            def col(row, c):
                return (row.get(c,'') if c and c != '(skip)' else '') or ''

            mapped_keys = {phone_col, name_col, email_col, company_col, '(skip)'}

            new_contacts = []
            for i, row in enumerate(rows):
                phone = clean_phone(col(row, phone_col))
                if not phone: continue
                extra = {k: v for k, v in row.items()
                         if k not in mapped_keys and v}
                new_contacts.append({
                    'phone':   phone,
                    'name':    col(row, name_col),
                    'email':   col(row, email_col),
                    'company': col(row, company_col),
                    'extra':   extra,
                    'status':  'pending',
                    'rownum':  i + 1,
                })

            if not new_contacts:
                self._toast('No valid contacts with phone numbers found', 'error')
                return

            if mode_var.get() == 'append':
                start = len(self.contacts)
                for c in new_contacts: c['rownum'] += start
                self.contacts.extend(new_contacts)
            else:
                self.contacts = new_contacts
                self.cur_idx  = -1

            self.filtered = self.contacts[:]
            dlg.destroy()
            self._render_contacts()
            self._update_queue_stats()
            self._toast(f'Imported {len(new_contacts)} contacts ✓', 'info')

        bf = tk.Frame(dlg, bg=C['bg'])
        bf.pack(pady=10)
        self._btn(bf, 'Cancel', dlg.destroy).pack(side='left', padx=5)
        ttk.Button(bf, text='✅  Import', style='Primary.TButton',
                   command=_confirm).pack(side='left', padx=5)

    # ──────────────────────────────────────────────────────────────────
    #  CONFERENCE DIALOG
    # ──────────────────────────────────────────────────────────────────
    def _open_conference(self):
        if self.sip.state not in ('active','conference'):
            self._toast('No active call', 'warn'); return

        dlg = tk.Toplevel(self)
        dlg.title('Add to Conference')
        dlg.configure(bg=C['bg'])
        dlg.geometry('360x160')
        dlg.resizable(False, False)

        tk.Label(dlg, text='Dial number to add to 3-way conference:',
                 bg=C['bg'], fg=C['txt1'],
                 font=('Segoe UI',9)).pack(padx=20, pady=(18,6), anchor='w')
        num_var = tk.StringVar()
        ttk.Entry(dlg, textvariable=num_var,
                  font=('Consolas',11), justify='center').pack(padx=20, fill='x')

        def _add():
            n = num_var.get().strip()
            if not n: self._toast('Enter a number', 'warn'); return
            self.sip.add_conference(n)
            dlg.destroy()
            self._toast(f'Calling conference party {n}…', 'info')

        bf = tk.Frame(dlg, bg=C['bg'])
        bf.pack(pady=14)
        self._btn(bf, 'Cancel', dlg.destroy).pack(side='left', padx=5)
        ttk.Button(bf, text='📞  Add', style='Primary.TButton',
                   command=_add).pack(side='left', padx=5)

    # ──────────────────────────────────────────────────────────────────
    #  TRANSFER DIALOG
    # ──────────────────────────────────────────────────────────────────
    def _open_transfer(self):
        if self.sip.state not in ('active','onhold'):
            self._toast('No active call to transfer', 'warn'); return

        dlg = tk.Toplevel(self)
        dlg.title('Transfer Call')
        dlg.configure(bg=C['bg'])
        dlg.geometry('380x220')
        dlg.resizable(False, False)

        tk.Label(dlg, text='Transfer to (extension or full number):',
                 bg=C['bg'], fg=C['txt1'],
                 font=('Segoe UI',9)).pack(padx=20, pady=(18,4), anchor='w')
        num_var = tk.StringVar()
        ttk.Entry(dlg, textvariable=num_var,
                  font=('Consolas',11), justify='center').pack(padx=20, fill='x')

        tk.Label(dlg, text='Transfer type:', bg=C['bg'],
                 fg=C['txt1'], font=('Segoe UI',9)).pack(padx=20,pady=(10,4),anchor='w')
        type_var = tk.StringVar(value='blind')
        tf = tk.Frame(dlg, bg=C['bg'])
        tf.pack(padx=20, anchor='w')
        ttk.Radiobutton(tf, text='Blind — immediately send caller',
                         variable=type_var, value='blind').pack(anchor='w')
        ttk.Radiobutton(tf, text='Attended — speak first, then connect',
                         variable=type_var, value='attended').pack(anchor='w')

        def _xfer():
            n = num_var.get().strip()
            if not n: self._toast('Enter a number', 'warn'); return
            dlg.destroy()
            if type_var.get() == 'blind':
                try:
                    self.sip.blind_transfer(n)
                    self._toast(f'Blind transfer to {n} initiated', 'info')
                except Exception as e:
                    self._toast(str(e), 'error')
            else:
                self.sip.attended_transfer_start(n)
                self._toast(f'Calling {n} for attended transfer…', 'info')

        bf = tk.Frame(dlg, bg=C['bg'])
        bf.pack(pady=12)
        self._btn(bf, 'Cancel', dlg.destroy).pack(side='left', padx=5)
        ttk.Button(bf, text='↗  Transfer', style='Primary.TButton',
                   command=_xfer).pack(side='left', padx=5)

    # ──────────────────────────────────────────────────────────────────
    #  SIP SETTINGS DIALOG
    # ──────────────────────────────────────────────────────────────────
    def _open_settings(self):
        dlg = tk.Toplevel(self)
        dlg.title('SIP Server Settings')
        dlg.configure(bg=C['bg'])
        dlg.geometry('420x440')
        dlg.resizable(False, False)

        fields = [
            ('Elastix/PBX IP or Hostname', 'server',        '192.168.1.100'),
            ('SIP Port (UDP)',              'port',          '5060'),
            ('Extension / Username',        'username',      '1001'),
            ('Password / Secret',           'password',      ''),
            ('Display Name',                'display_name',  'BPO Agent'),
            ('Local SIP Port',              'local_sip_port','5080'),
        ]
        entries = {}
        for label, key, placeholder in fields:
            tk.Label(dlg, text=label, bg=C['bg'], fg=C['txt1'],
                     font=('Segoe UI',9)).pack(padx=20, pady=(10,2), anchor='w')
            show = '*' if key == 'password' else ''
            var  = tk.StringVar(value=self.cfg[key])
            e    = ttk.Entry(dlg, textvariable=var, show=show)
            e.pack(padx=20, fill='x')
            if not self.cfg[key]:
                e.insert(0, placeholder)
                e.config(foreground=C['txt2'])
                def _focus_in(ev, entry=e, ph=placeholder):
                    if entry.get() == ph:
                        entry.delete(0,'end')
                        entry.config(foreground=C['txt0'])
                e.bind('<FocusIn>', _focus_in)
            entries[key] = var

        # warning note
        note = tk.Label(dlg,
            text='⚠  Uses standard UDP SIP — no changes needed on Elastix 2.5',
            bg=C['bg'], fg=C['green'],
            font=('Segoe UI',8), wraplength=380)
        note.pack(padx=20, pady=(8,4))

        def _save_and_connect():
            for key, var in entries.items():
                self.cfg[key] = var.get().strip()
            self.cfg.save()
            try:
                self.sip_dot.config(fg=C['yellow'])
                self.sip_lbl.config(text='Connecting…')
                threading.Thread(target=self._do_connect, daemon=True).start()
                dlg.destroy()
            except Exception as e:
                self._toast(str(e), 'error')

        def _disconnect():
            self.sip.disconnect()
            dlg.destroy()

        bf = tk.Frame(dlg, bg=C['bg'])
        bf.pack(pady=14)
        self._btn(bf, 'Cancel',     dlg.destroy).pack(side='left', padx=4)
        self._btn(bf, 'Disconnect', _disconnect,
                  style='Danger.TButton').pack(side='left', padx=4)
        ttk.Button(bf, text='🔗  Connect', style='Primary.TButton',
                   command=_save_and_connect).pack(side='left', padx=4)

    def _do_connect(self):
        try:
            self.sip.connect(
                server    = self.cfg['server'],
                port      = self.cfg['port'],
                user      = self.cfg['username'],
                password  = self.cfg['password'],
                sip_port  = self.cfg['local_sip_port'],
            )
        except Exception as e:
            self._ui_q.put(('error', str(e)))
            self._ui_q.put(('disconnected', None))

    # ──────────────────────────────────────────────────────────────────
    #  TOAST NOTIFICATIONS
    # ──────────────────────────────────────────────────────────────────
    def _toast(self, msg, kind='info', duration=3500):
        col = {'info': C['blue'], 'warn': C['yellow'],
               'error': C['red'], 'success': C['green']}.get(kind, C['blue'])
        w = tk.Toplevel(self)
        w.overrideredirect(True)
        w.attributes('-topmost', True)
        self.update_idletasks()
        x = self.winfo_x() + self.winfo_width()  - 290
        y = self.winfo_y() + self.winfo_height() - 70
        w.geometry(f'270x44+{x}+{y}')
        w.configure(bg=C['bg1'])
        tk.Frame(w, bg=col, width=3).pack(side='left', fill='y')
        tk.Label(w, text=msg, bg=C['bg1'], fg=C['txt0'],
                 font=('Segoe UI', 9), wraplength=240,
                 justify='left').pack(side='left', padx=10)
        w.after(duration, w.destroy)

    # ──────────────────────────────────────────────────────────────────
    #  CLOSE
    # ──────────────────────────────────────────────────────────────────
    def _on_close(self):
        self.cfg['auto_dial']  = self.auto_dial.get()
        self.cfg['dial_delay'] = self.delay_var.get()
        self.cfg.save()
        self.sip.disconnect()
        self.notes_db.close()
        self.destroy()


# ══════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════
def main():
    if not HAS_SIP:
        import tkinter.messagebox as mb
        root = tk.Tk()
        root.withdraw()
        mb.showwarning(
            'Missing dependency',
            'pyVoIP is not installed.\n\n'
            'Run the following in your terminal/command prompt:\n\n'
            '    pip install pyvoip PyAudio numpy\n\n'
            'Then restart the dialer.')
        root.destroy()
        return

    if not HAS_AUDIO:
        import tkinter.messagebox as mb
        root = tk.Tk()
        root.withdraw()
        mb.showwarning(
            'Missing dependency',
            'PyAudio or numpy is not installed.\n'
            'Audio will not work.\n\n'
            'Run:  pip install PyAudio numpy\n\n'
            'Continuing without audio…')
        root.destroy()

    app = App()
    app.mainloop()


if __name__ == '__main__':
    main()
