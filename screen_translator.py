# -*- coding: utf-8 -*-
"""
画面に表示された英語を、その場に日本語を重ねて自動翻訳する常時オーバーレイツール。
 
構成:
  - 画面キャプチャ : mss
  - OCR           : RapidOCR (onnxruntime, モデル同梱・管理者不要, ローカル)
  - 翻訳           : deep-translator 経由の Google 翻訳 (en->ja, 要ネット接続)
  - オーバーレイ   : tkinter の透明・最前面・クリック透過ウィンドウ
 
オーバーレイは画面キャプチャから除外しない（WDA_EXCLUDEFROMCAPTUREは使わない）。
そのため OBS・録画・スクショなど外部のキャプチャにも翻訳が写る。
「翻訳の上にさらに翻訳がかかる」フィードバックは、画面が変化したときだけ一瞬
帯を消して実画面を撮り直す方式で防ぐ。静止中は帯を消さないので瞬きは起きない。
 
使い方:
  python screen_translator.py                # 主モニタを翻訳
  python screen_translator.py --monitor 2    # 2番目のモニタ
  python screen_translator.py --interval 800 # 更新間隔(ミリ秒)
 
設定:
  スクリプトと同じフォルダの config.json で既定値を変更できる（無くても動く）。
  優先順位: コマンドライン引数 > config.json > 組み込み既定値
"""
 
import argparse
import json
from pathlib import Path
import queue
import threading
import time
import ctypes
 
import numpy as np
import mss
import tkinter as tk
from tkinter import font as tkfont
 
# 透明色（このRGBのピクセルは完全に透明＝下が見える）
TRANSPARENT = "#FF00FF"
 
# 組み込み既定値。config.json とコマンドライン引数で上書きできる。
DEFAULT_CONFIG = {
    "monitor": None,          # 対象モニタ番号(1〜)。None ならメインモニタ
    "interval": 1000,         # 更新間隔(ミリ秒)
    "min_conf": 0.5,          # OCR信頼度の下限 (0〜1)
    "diff_threshold": 3.0,    # フレーム差分のしきい値 (0で常にOCR)
    "use_dml": True,          # OCRをDirectML(GPU)で実行
    "font": "Yu Gothic UI",   # オーバーレイの日本語フォント
    "source_lang": "en",      # 翻訳元言語 (Google翻訳の言語コード)
    "target_lang": "ja",      # 翻訳先言語 (Google翻訳の言語コード)
}
 
 
def load_config():
    """スクリプトと同じフォルダの config.json を読み、既定値に重ねて返す。
 
    設定ミスで起動不能になるのが最悪なので、ファイルが無い・壊れている場合は
    警告を出して組み込み既定値で続行する。未知のキーは無視して知らせる。
    """
    cfg = dict(DEFAULT_CONFIG)
    path = Path(__file__).resolve().parent / "config.json"
    if not path.exists():
        return cfg
    try:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"config.json を読み込めませんでした（既定値で続行）: {e}")
        return cfg
    if not isinstance(user, dict):
        print("config.json の形式が不正です（既定値で続行）: 最上位はオブジェクトにしてください")
        return cfg
    unknown = sorted(set(user) - set(cfg))
    if unknown:
        print(f"config.json の未知のキーを無視します: {', '.join(unknown)}")
    for k in cfg:
        if k in user:
            cfg[k] = user[k]
    return cfg
 
# Win32 定数
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
 
# SetWindowPos 用
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
 
 
def set_dpi_awareness():
    """プロセスを Per-Monitor-V2 DPI aware にする。
 
    これをしないと、モニタ倍率が異なる環境で mss(物理ピクセル)と tkinter の
    座標系がずれ、オーバーレイが正しいモニタ・正しい位置に出ない。
    mss/tkinter の初期化より前に呼ぶこと。倍率が全モニタ100%でも無害。
    """
    try:  # Windows 10 1703+
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:  # Windows 8.1+ : PROCESS_PER_MONITOR_DPI_AWARE = 2
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
 
 
def _hwnd_of(win):
    """tkinter ウィンドウの実際の HWND を返す。"""
    user32 = ctypes.windll.user32
    return user32.GetParent(win.winfo_id()) or win.winfo_id()
 
 
def move_window(win, left, top, width, height):
    """ウィンドウを実ピクセル座標へ強制配置する。
 
    overrideredirect ウィンドウは tkinter の geometry() だけでは別モニタへ
    正しく移動しないことがあるため、Win32 の SetWindowPos で確実に配置する。
    """
    user32 = ctypes.windll.user32
    user32.SetWindowPos.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    user32.SetWindowPos.restype = ctypes.c_bool
    user32.SetWindowPos(
        _hwnd_of(win), ctypes.c_void_p(HWND_TOPMOST),
        int(left), int(top), int(width), int(height),
        SWP_NOACTIVATE | SWP_SHOWWINDOW,
    )
 
 
def is_english(s: str) -> bool:
    """英語(ラテン文字主体)の行だけを翻訳対象にするための判定。"""
    s = s.strip()
    if len(s) < 2:
        return False
    letters = sum(1 for c in s if c.isascii() and c.isalpha())
    cjk = 0
    for c in s:
        o = ord(c)
        if (0x3000 <= o <= 0x9FFF) or (0xAC00 <= o <= 0xD7A3) or (0xFF00 <= o <= 0xFFEF):
            cjk += 1
    return letters >= 2 and cjk == 0
 
 
class Translator:
    """deep-translator 経由の Google 翻訳 (en->ja)。結果はキャッシュして再翻訳を避ける。
 
    オンライン（要ネット接続）。ネットワーク失敗時は原文をそのまま返し、
    失敗はキャッシュしない（接続が復帰したら再翻訳できるように）。
    """
 
    def __init__(self, source="en", target="ja"):
        from deep_translator import GoogleTranslator
        self._g = GoogleTranslator(source=source, target=target)
        self.cache = {}
 
    def translate(self, text: str) -> str:
        if text in self.cache:
            return self.cache[text]
        try:
            out = self._g.translate(text)
        except Exception:
            return text  # 失敗はキャッシュしない（次回リトライできるように）
        if not out:
            return text
        self.cache[text] = out
        return out
 
 
class State:
    """ワーカースレッドとUIスレッド間で共有する状態。"""
 
    def __init__(self):
        self.lock = threading.Lock()
        self.items = []          # [(x1, y1, x2, y2, jp_text), ...]
        self.status = "初期化中…"
        self.last_ms = 0
 
    def set_items(self, items, dt):
        with self.lock:
            self.items = items
            self.last_ms = int(dt * 1000)
 
    def get_items(self):
        with self.lock:
            return list(self.items), self.last_ms
 
    def set_status(self, s):
        with self.lock:
            self.status = s
 
    def get_status(self):
        with self.lock:
            return self.status
 
 
class Worker(threading.Thread):
    """キャプチャ画像を受け取り、OCR -> 翻訳して State を更新する。"""
 
    def __init__(self, cap_q: "queue.Queue", state: State, min_conf: float,
                 diff_threshold: float, use_dml: bool = True,
                 source_lang: str = "en", target_lang: str = "ja"):
        super().__init__(daemon=True)
        self.cap_q = cap_q
        self.state = state
        self.min_conf = min_conf
        self.diff_threshold = diff_threshold
        self.use_dml = use_dml
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.running = True
        self.prev_small = None  # 直近にOCRしたフレームの縮小グレースケール画像
        # UIスレッドへ「帯を消したクリーンなフレームを撮ってくれ」と要求するフラグ。
        # 起動直後は True（最初のOCRを走らせるため）。
        self.request_clean = True
 
    def run(self):
        self.state.set_status("OCRエンジンを読み込み中…")
        from rapidocr_onnxruntime import RapidOCR
        # use_dml=True のとき DirectML(GPU) を各モジュールで有効化。
        # DirectMLが使えない環境では rapidocr が自動的にCPUへフォールバックする。
        ocr = RapidOCR(
            det_use_dml=self.use_dml,
            cls_use_dml=self.use_dml,
            rec_use_dml=self.use_dml,
        )
        self.state.set_status("翻訳モデルを読み込み中…")
        tr = Translator(self.source_lang, self.target_lang)
        # ウォームアップ（初回呼び出しの遅延を先に消化）
        try:
            tr.translate("ready")
        except Exception:
            pass
        self.state.set_status("実行中")
 
        while self.running:
            try:
                item = self.cap_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            arr, is_clean = item
 
            # --- 通常フレーム（帯を含む画面）: 変化検出のみ ---
            # 前回の基準フレームとの画素差がしきい値未満なら画面に変化なしとみなし、
            # 何もしない（帯を消さないので瞬きが起きず、前回訳をそのまま維持）。
            # 変化があれば「クリーンなフレームが欲しい」と要求だけ立てて次へ。
            if not is_clean:
                small = downscale_gray(arr)
                if self.prev_small is None:
                    # OCR直後の再基準化。今の帯を含む画面を新しい基準にする。
                    self.prev_small = small
                    continue
                diff = float(np.abs(small - self.prev_small).mean())
                if diff < self.diff_threshold:
                    self.state.set_status("変化なし（待機中）")
                    continue
                self.request_clean = True  # 変化あり → 再スキャンを要求
                self.state.set_status("変化を検出、再スキャン中…")
                continue
 
            # --- クリーンフレーム（帯を消して撮った実画面）: ここでだけOCR ---
            # 自分の翻訳を撮り込まないので、フィードバック（点滅）が起きない。
            self.request_clean = False
            self.prev_small = None  # 次の通常フレームで新しい帯を基準に取り直す
            self.state.set_status("実行中")
            t0 = time.time()
            try:
                res, _ = ocr(arr)
            except Exception:
                continue
 
            items = []
            if res:
                for box, text, conf in res:
                    try:
                        if conf is not None and float(conf) < self.min_conf:
                            continue
                    except (TypeError, ValueError):
                        pass
                    text = text.strip()
                    if not is_english(text):
                        continue
                    xs = [p[0] for p in box]
                    ys = [p[1] for p in box]
                    jp = tr.translate(text)
                    items.append(
                        (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)), jp)
                    )
            self.state.set_items(items, time.time() - t0)
 
 
def downscale_gray(arr, size=64):
    """BGR画像を numpy だけでグレースケール化し、size×size に縮小する。
 
    差分判定専用の粗い縮小（最近傍サンプリング）。OpenCV/PIL は使わない。
    """
    gray = arr.mean(axis=2)  # (h, w) BGR3チャンネルの平均
    h, w = gray.shape
    ys = np.linspace(0, h - 1, size).astype(np.intp)
    xs = np.linspace(0, w - 1, size).astype(np.intp)
    return gray[np.ix_(ys, xs)].astype(np.float32)
 
 
def push_latest(cap_q: "queue.Queue", arr):
    """キューには最新の1枚だけを保持する（古い画像は捨てる）。"""
    try:
        cap_q.get_nowait()
    except queue.Empty:
        pass
    try:
        cap_q.put_nowait(arr)
    except queue.Full:
        pass
 
 
def apply_overlay_styles(win):
    """ウィンドウをクリック透過＋ツールウィンドウ化する。
 
    画面キャプチャからの除外(WDA_EXCLUDEFROMCAPTURE)は行わない。
    そのため OBS・録画・スクショなど外部のキャプチャにも翻訳が写る。
    自分自身の翻訳を再OCRしてしまうフィードバックは、グラブ直前に
    キャンバスを一瞬クリアする方式(tick内)で防いでいる。
    """
    user32 = ctypes.windll.user32
    hwnd = _hwnd_of(win)
    cur = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(
        hwnd,
        GWL_EXSTYLE,
        cur | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW,
    )
 
 
def main():
    # mss / tkinter の初期化より前に DPI aware 化しておく（全モニタで座標を一致）
    set_dpi_awareness()
 
    cfg = load_config()
    parser = argparse.ArgumentParser(description="画面の英語を日本語に常時オーバーレイ翻訳")
    parser.add_argument("--monitor", type=int, default=cfg["monitor"],
                        help="対象モニタ番号(mss基準, 1〜)。未指定ならメインモニタを自動選択し、"
                             "複数ある場合は操作パネルで切替可能")
    parser.add_argument("--interval", type=int, default=cfg["interval"], help="更新間隔(ミリ秒)")
    parser.add_argument("--min-conf", type=float, default=cfg["min_conf"], help="OCR信頼度の下限 (0〜1)")
    parser.add_argument("--diff-threshold", type=float, default=cfg["diff_threshold"],
                        help="フレーム差分のしきい値。これ未満ならOCRをスキップ (0で常にOCR)")
    parser.add_argument("--use-dml", dest="use_dml", action="store_true", default=cfg["use_dml"],
                        help="OCRをDirectML(GPU)で実行する (既定: 有効。不可時は自動でCPUにフォールバック)")
    parser.add_argument("--no-use-dml", dest="use_dml", action="store_false",
                        help="DirectMLを使わずCPUで実行する")
    parser.add_argument("--font", default=cfg["font"], help="オーバーレイのフォント名")
    parser.add_argument("--source-lang", default=cfg["source_lang"],
                        help="翻訳元言語コード (既定: en)。注意: 画面上の検出フィルタは"
                             "ラテン文字主体の行を対象とするため、非ラテン文字の言語は未対応")
    parser.add_argument("--target-lang", default=cfg["target_lang"],
                        help="翻訳先言語コード (既定: ja)。フォントが対象言語に対応している必要あり")
    args = parser.parse_args()
 
    sct = mss.mss()
    monitors = sct.monitors
    phys = monitors[1:]  # [0]は全モニタ結合なので除外、[1:]が個々の物理モニタ
    if not phys:
        print("モニタが見つかりません")
        return
 
    def is_primary(m):
        # mss が is_primary を持たない環境もあるので、原点(0,0)も主モニタ判定に使う
        return bool(m.get("is_primary")) or (m["left"] == 0 and m["top"] == 0)
 
    # 既定は「メイン(プライマリ)モニタ」。--monitor 指定時のみそれを優先。
    if args.monitor is not None:
        if args.monitor < 1 or args.monitor >= len(monitors):
            print(f"モニタ {args.monitor} は存在しません。利用可能: 1〜{len(monitors) - 1}")
            return
        sel_idx = args.monitor - 1  # phys のインデックスへ変換
    else:
        sel_idx = next((i for i, m in enumerate(phys) if is_primary(m)), 0)
 
    # 稼働中に切替できるよう、対象モニタは可変ホルダーに入れて共有する
    cur = {}
 
    def set_target(i):
        m = phys[i]
        cur.update(idx=i, mon=m,
                   left=m["left"], top=m["top"],
                   width=m["width"], height=m["height"])
 
    set_target(sel_idx)
    left, top = cur["left"], cur["top"]
    width, height = cur["width"], cur["height"]
 
    state = State()
    cap_q: "queue.Queue" = queue.Queue(maxsize=1)
    worker = Worker(cap_q, state, args.min_conf, args.diff_threshold, args.use_dml,
                    args.source_lang, args.target_lang)
    worker.start()
 
    # ---- 操作パネル（小さな通常ウィンドウ） ----
    root = tk.Tk()
    root.title("画面翻訳")
    root.attributes("-topmost", True)
    root.geometry(f"+{left + 20}+{top + 20}")
    root.resizable(False, False)
    paused = {"v": False}
 
    status_var = tk.StringVar(value="初期化中…")
    tk.Label(root, textvariable=status_var, width=34, anchor="w").grid(
        row=0, column=0, columnspan=2, padx=8, pady=(8, 4)
    )
 
    # ---- モニター選択（複数ある場合のみ表示） ----
    def mon_label(i):
        m = phys[i]
        tag = "（メイン）" if is_primary(m) else ""
        return f"モニター{i + 1}{tag}  {m['width']}×{m['height']}"
 
    mon_options = [mon_label(i) for i in range(len(phys))]
    mon_var = tk.StringVar(value=mon_options[sel_idx])
    if len(phys) > 1:
        sel_row = tk.Frame(root)
        sel_row.grid(row=1, column=0, columnspan=2, padx=8, pady=(0, 6), sticky="we")
        tk.Label(sel_row, text="翻訳するモニター:").pack(side="left")
        tk.OptionMenu(
            sel_row, mon_var, *mon_options,
            command=lambda _=None: switch_monitor(mon_options.index(mon_var.get())),
        ).pack(side="left", padx=(6, 0))
 
    def toggle_pause():
        paused["v"] = not paused["v"]
        pause_btn.config(text="再開" if paused["v"] else "一時停止")
 
    def quit_app():
        worker.running = False
        push_latest(cap_q, None)
        root.destroy()
 
    pause_btn = tk.Button(root, text="一時停止", width=12, command=toggle_pause)
    pause_btn.grid(row=2, column=0, padx=8, pady=(0, 8))
    tk.Button(root, text="終了", width=12, command=quit_app).grid(
        row=2, column=1, padx=8, pady=(0, 8)
    )
    root.protocol("WM_DELETE_WINDOW", quit_app)
 
    # ---- オーバーレイ（全画面・透明・クリック透過） ----
    overlay = tk.Toplevel(root)
    overlay.overrideredirect(True)
    overlay.geometry(f"{width}x{height}+{left}+{top}")
    overlay.attributes("-topmost", True)
    overlay.config(bg=TRANSPARENT)
    overlay.attributes("-transparentcolor", TRANSPARENT)
    canvas = tk.Canvas(overlay, bg=TRANSPARENT, highlightthickness=0, bd=0)
    canvas.pack(fill="both", expand=True)
 
    overlay.update_idletasks()
    apply_overlay_styles(overlay)
    # overrideredirect ウィンドウは geometry() だけでは別モニタへ正しく移動しない
    # ことがあるため、実ピクセル座標へ強制配置する（=どのモニタでも動く）。
    move_window(overlay, left, top, width, height)
    root.update_idletasks()
 
    fonts = {}
 
    def get_font(size):
        if size not in fonts:
            fonts[size] = tkfont.Font(family=args.font, size=size)
        return fonts[size]
 
    def redraw():
        items, _ = state.get_items()
        canvas.delete("all")
        for x1, y1, x2, y2, jp in items:
            bh = max(1, y2 - y1)
            size = max(9, min(40, int(bh * 0.6)))
            f = get_font(size)
            # 元の英語を隠す白背景 + 日本語
            canvas.create_rectangle(x1, y1, x2 + 2, y2 + 2, fill="white", outline="")
            canvas.create_text(
                x1 + 3, (y1 + y2) // 2, anchor="w", text=jp, fill="black", font=f
            )
 
    def switch_monitor(i):
        """稼働中に対象モニタを切り替える。オーバーレイを移動し、検出状態を初期化。"""
        if i == cur["idx"]:
            return
        set_target(i)
        l, t, w, h = cur["left"], cur["top"], cur["width"], cur["height"]
        overlay.geometry(f"{w}x{h}+{l}+{t}")
        overlay.update_idletasks()
        move_window(overlay, l, t, w, h)          # 実ピクセル座標へ確実に移動
        root.geometry(f"+{l + 20}+{t + 20}")      # 操作パネルも新モニタ側へ
        canvas.delete("all")
        state.set_items([], 0.0)                  # 前モニタの訳を消す
        worker.prev_small = None                  # 差分基準をリセット
        worker.request_clean = True               # 新モニタを即スキャン
 
    def tick():
        if not paused["v"]:
            try:
                if worker.request_clean:
                    # 変化を検出したとき等のみ: 一瞬だけ帯を消して実画面(英語)を撮る。
                    # ここでのOCRだけがフィードバック防止＆変化の再スキャンを担う。
                    canvas.delete("all")
                    overlay.update()
                    time.sleep(0.02)  # DWM合成が透明を反映するまで待つ
                    shot = sct.grab(cur["mon"])
                    arr = np.array(shot)[:, :, :3]  # BGRA -> BGR
                    push_latest(cap_q, (arr, True))
                else:
                    # 通常時: 帯を消さずそのまま撮る（瞬きなし・OBS等にも翻訳が写る）。
                    shot = sct.grab(cur["mon"])
                    arr = np.array(shot)[:, :, :3]  # BGRA -> BGR
                    push_latest(cap_q, (arr, False))
            except Exception:
                pass
        redraw()
        _, ms = state.get_items()
        label = "一時停止中" if paused["v"] else state.get_status()
        status_var.set(f"{label}   処理: {ms}ms")
        root.after(args.interval, tick)
 
    tick()
    root.mainloop()
 
 
if __name__ == "__main__":
    main()
 