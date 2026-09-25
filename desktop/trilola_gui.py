import os
import glob
import json
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
import matplotlib.image as mpimg
from matplotlib.colors import LinearSegmentedColormap

# Versuch, die Bibliothek für kollisionsfreie Labels zu laden
try:
    from adjustText import adjust_text

    HAS_ADJUST_TEXT = True
except ImportError:
    HAS_ADJUST_TEXT = False
    print("HINWEIS: 'adjustText' ist nicht installiert. Labels könnten sich überlagern.")
    print("Installiere es mit: pip install adjustText")

CONFIG_DIR = "config"
VIEWER_CONFIG_FILE = os.path.join(CONFIG_DIR, "viewer_config.json")
# Sensoren, Heatmap und Grundriss liegen beim Tracker (bt_tracker/config).
_HERE = os.path.dirname(os.path.abspath(__file__))
TRACKER_CONFIG_DIR = os.path.normpath(os.path.join(_HERE, "..", "bt_tracker", "config"))
DATA_DIR = TRACKER_CONFIG_DIR if os.path.isdir(TRACKER_CONFIG_DIR) else CONFIG_DIR
FLOORPLAN_FILE = os.path.join(DATA_DIR, "floorplan.json")
NON_SENSOR_FILES = {"viewer_config.json", "floorplan.json", "tracker_state.json", "calibration_points.json"}

# --- EIGENE FARBSKALA DEFINIEREN ---
# Verlauf: Transparent (0 Dämpfung) -> Rot -> Dunkelrot -> Schwarz (Starke Dämpfung)
trilola_cmap = LinearSegmentedColormap.from_list("TriLolaHeat", [(1, 1, 1, 0), "red", "darkred", "black"])


class TriLolaViewerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TriLola RF-Tomographie & Tracker GUI")
        self.geometry("1300x850")

        # --- Status-Variablen für das Hintergrundbild ---
        self.bg_image_path = ""
        self.bg_img_original = None
        self.bg_img_data = None
        self.bg_offset_x = 0.0
        self.bg_offset_y = 0.0
        self.bg_scale = 1.0
        self.bg_rotation = 0
        self.bg_flip_h = False
        self.bg_flip_v = False

        # --- Transparenz Variablen ---
        self.alpha_heatmap = tk.DoubleVar(value=0.7)
        self.alpha_bg = tk.DoubleVar(value=0.8)

        # --- Status-Variablen für Maus-Interaktionen ---
        self.is_edit_mode = tk.BooleanVar(value=False)
        self.is_dragging = False
        self.last_mouse_x = None
        self.last_mouse_y = None

        # --- Grundriss (Wände & Räume) ---
        self.floorplan = self.load_floorplan()
        self.draw_mode = tk.StringVar(value="aus")
        self.wall_db = tk.DoubleVar(value=float(self.floorplan.get("default_wall_db", 5.0)))
        self.pending_points = []
        self.history = []  # ("wall"|"room", index) für Rückgängig

        # --- Matplotlib Objekte ---
        self.fig, self.ax = plt.subplots(figsize=(8, 6))
        self.canvas = None
        self.img_plot = None
        self.heatmap_plot = None

        self.setup_ui()
        self.load_viewer_config()
        self.reload_data_and_draw()

    def setup_ui(self):
        # --- LINKE SEITENLEISTE ---
        sidebar = tk.Frame(self, width=280, bg="#f0f0f0", padx=10, pady=10)
        sidebar.pack(side=tk.LEFT, fill=tk.Y)

        lbl_title = tk.Label(sidebar, text="TriLola Steuerung", font=("Arial", 14, "bold"), bg="#f0f0f0")
        lbl_title.pack(pady=(0, 15))

        # --- BLOCK 1: Heatmap & Tracker ---
        frame_tracker = tk.LabelFrame(sidebar, text="Ansicht & Daten", bg="#f0f0f0", padx=5, pady=5)
        frame_tracker.pack(fill=tk.X, pady=5)

        tk.Button(frame_tracker, text="Daten neu laden", command=self.reload_data_and_draw).pack(fill=tk.X, pady=5)

        tk.Label(frame_tracker, text="Heatmap Transparenz:", bg="#f0f0f0").pack(anchor=tk.W)
        tk.Scale(frame_tracker, from_=0.0, to=1.0, resolution=0.05, orient=tk.HORIZONTAL,
                 variable=self.alpha_heatmap, command=self.on_alpha_changed, bg="#f0f0f0").pack(fill=tk.X)

        # --- BLOCK 2: Hintergrundbild ---
        frame_bg = tk.LabelFrame(sidebar, text="Grundriss-Steuerung", bg="#f0f0f0", padx=5, pady=5)
        frame_bg.pack(fill=tk.X, pady=5)

        tk.Button(frame_bg, text="Bild auswählen...", command=self.load_image).pack(fill=tk.X, pady=5)

        # Rotation
        frame_rot = tk.Frame(frame_bg, bg="#f0f0f0")
        frame_rot.pack(fill=tk.X, pady=2)
        tk.Button(frame_rot, text="↶ 90°", command=lambda: self.rotate_image(-90)).pack(side=tk.LEFT, expand=True,
                                                                                        fill=tk.X, padx=2)
        tk.Button(frame_rot, text="90° ↷", command=lambda: self.rotate_image(90)).pack(side=tk.RIGHT, expand=True,
                                                                                       fill=tk.X, padx=2)

        # Spiegeln
        frame_flip = tk.Frame(frame_bg, bg="#f0f0f0")
        frame_flip.pack(fill=tk.X, pady=2)
        tk.Button(frame_flip, text="↔ Horizontal", command=lambda: self.flip_image('h')).pack(side=tk.LEFT, expand=True,
                                                                                              fill=tk.X, padx=2)
        tk.Button(frame_flip, text="↕ Vertikal", command=lambda: self.flip_image('v')).pack(side=tk.RIGHT, expand=True,
                                                                                            fill=tk.X, padx=2)

        tk.Label(frame_bg, text="Bild Transparenz:", bg="#f0f0f0").pack(anchor=tk.W, pady=(5, 0))
        tk.Scale(frame_bg, from_=0.0, to=1.0, resolution=0.05, orient=tk.HORIZONTAL,
                 variable=self.alpha_bg, command=self.on_alpha_changed, bg="#f0f0f0").pack(fill=tk.X)

        # --- BLOCK 3: Positionierung (Edit Mode) ---
        frame_edit = tk.LabelFrame(sidebar, text="Bild platzieren", bg="#f0f0f0", padx=5, pady=5)
        frame_edit.pack(fill=tk.X, pady=5)

        tk.Checkbutton(frame_edit, text="Editiermodus aktiv",
                       variable=self.is_edit_mode, bg="#f0f0f0",
                       command=self.toggle_edit_mode).pack(anchor=tk.W, pady=2)

        tk.Label(frame_edit, text="Im Editiermodus:\n- Linke Maus = Verschieben\n- Mausrad = Zoomen",
                 justify=tk.LEFT, fg="#555555", bg="#f0f0f0").pack(anchor=tk.W, pady=2)

        self.lbl_info = tk.Label(frame_edit, text="X: 0.0 | Y: 0.0\nScale: 1.0 | Rot: 0°",
                                 justify=tk.LEFT, font=("Consolas", 9), bg="#e8e8e8", padx=5, pady=5)
        self.lbl_info.pack(fill=tk.X, pady=5)

        # --- BLOCK 3b: Grundriss ---
        frame_fp = tk.LabelFrame(sidebar, text="Grundriss (Wände & Räume)", bg="#f0f0f0", padx=5, pady=5)
        frame_fp.pack(fill=tk.X, pady=5)
        for value, label in (("aus", "Aus"), ("wand", "Wand zeichnen (2 Klicks)"), ("raum", "Raum zeichnen (Ecken klicken)")):
            tk.Radiobutton(frame_fp, text=label, value=value, variable=self.draw_mode, bg="#f0f0f0",
                           command=self.on_draw_mode_changed).pack(anchor=tk.W)
        row = tk.Frame(frame_fp, bg="#f0f0f0")
        row.pack(fill=tk.X, pady=2)
        tk.Label(row, text="Wanddämpfung dB:", bg="#f0f0f0").pack(side=tk.LEFT)
        tk.Entry(row, textvariable=self.wall_db, width=6).pack(side=tk.LEFT, padx=4)
        tk.Button(frame_fp, text="Raum abschließen", command=self.finish_room).pack(fill=tk.X, pady=2)
        tk.Button(frame_fp, text="Letztes Element löschen", command=self.undo_floorplan).pack(fill=tk.X, pady=2)
        tk.Button(frame_fp, text="Grundriss speichern", command=self.save_floorplan).pack(fill=tk.X, pady=2)
        tk.Label(frame_fp, text="Tipp: Türen als Lücken lassen.\nEndpunkte rasten an (15 cm).",
                 justify=tk.LEFT, fg="#555555", bg="#f0f0f0").pack(anchor=tk.W)

        # --- BLOCK 4: Speichern ---
        tk.Button(sidebar, text="💾 Setup speichern", font=("Arial", 10, "bold"),
                  bg="#d9ead3", command=self.save_viewer_config).pack(fill=tk.X, pady=20)

        # --- RECHTER ZEICHENBEREICH ---
        main_frame = tk.Frame(self)
        main_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        self.canvas = FigureCanvasTkAgg(self.fig, master=main_frame)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.toolbar = NavigationToolbar2Tk(self.canvas, main_frame)
        self.toolbar.update()

        # Matplotlib Events anbinden
        self.canvas.mpl_connect("scroll_event", self.on_scroll)
        self.canvas.mpl_connect("button_press_event", self.on_press)
        self.canvas.mpl_connect("button_release_event", self.on_release)
        self.canvas.mpl_connect("motion_notify_event", self.on_motion)

    # ==========================================
    #        KONFIGURATION SPEICHERN & LADEN
    # ==========================================
    def save_viewer_config(self):
        config = {
            "bg_image_path": self.bg_image_path,
            "bg_offset_x": self.bg_offset_x,
            "bg_offset_y": self.bg_offset_y,
            "bg_scale": self.bg_scale,
            "bg_rotation": self.bg_rotation,
            "bg_flip_h": self.bg_flip_h,
            "bg_flip_v": self.bg_flip_v,
            "alpha_heatmap": self.alpha_heatmap.get(),
            "alpha_bg": self.alpha_bg.get()
        }
        os.makedirs(CONFIG_DIR, exist_ok=True)
        try:
            with open(VIEWER_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4)
            messagebox.showinfo("Gespeichert", "Das Hintergrund-Setup wurde erfolgreich gespeichert!")
        except Exception as e:
            messagebox.showerror("Fehler", f"Speichern fehlgeschlagen:\n{e}")

    def load_viewer_config(self):
        if not os.path.exists(VIEWER_CONFIG_FILE):
            return
        try:
            with open(VIEWER_CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)

            self.bg_image_path = config.get("bg_image_path", "")
            self.bg_offset_x = config.get("bg_offset_x", 0.0)
            self.bg_offset_y = config.get("bg_offset_y", 0.0)
            self.bg_scale = config.get("bg_scale", 1.0)
            self.bg_rotation = config.get("bg_rotation", 0)
            self.bg_flip_h = config.get("bg_flip_h", False)
            self.bg_flip_v = config.get("bg_flip_v", False)
            self.alpha_heatmap.set(config.get("alpha_heatmap", 0.7))
            self.alpha_bg.set(config.get("alpha_bg", 0.8))

            if self.bg_image_path and os.path.exists(self.bg_image_path):
                self.bg_img_original = mpimg.imread(self.bg_image_path)
                self.apply_transformations()
        except Exception as e:
            print(f"Konnte viewer_config.json nicht laden: {e}")

    # ==========================================
    #        BILD & ZEICHNEN
    # ==========================================
    def toggle_edit_mode(self):
        if self.is_edit_mode.get():
            if self.toolbar.mode:
                self.toolbar.pan() if self.toolbar.mode == "pan/zoom" else self.toolbar.zoom()
        self.update_info_labels()

    def update_info_labels(self):
        flip_h_str = "Ja" if self.bg_flip_h else "Nein"
        flip_v_str = "Ja" if self.bg_flip_v else "Nein"
        info_txt = (f"X: {self.bg_offset_x:.1f} | Y: {self.bg_offset_y:.1f}\n"
                    f"Scale: {self.bg_scale:.3f} | Rot: {self.bg_rotation}°\n"
                    f"Flip H: {flip_h_str} | Flip V: {flip_v_str}")
        self.lbl_info.config(text=info_txt)

    def on_alpha_changed(self, event=None):
        if self.img_plot is not None:
            self.img_plot.set_alpha(self.alpha_bg.get())
        if self.heatmap_plot is not None:
            self.heatmap_plot.set_alpha(self.alpha_heatmap.get())
        self.canvas.draw_idle()

    def load_image(self):
        file_path = filedialog.askopenfilename(
            title="Grundriss auswählen",
            filetypes=[("Bilder", "*.png *.jpg *.jpeg")]
        )
        if file_path:
            self.bg_image_path = file_path
            try:
                self.bg_img_original = mpimg.imread(self.bg_image_path)
                self.bg_rotation = 0
                self.bg_flip_h = False
                self.bg_flip_v = False
                self.apply_transformations()

                # Auto-Zentrierung
                sensors = self.load_sensors()
                if sensors:
                    min_x, max_x = min(s['x'] for s in sensors), max(s['x'] for s in sensors)
                    min_y, max_y = min(s['y'] for s in sensors), max(s['y'] for s in sensors)
                    w_sensors = max(max_x - min_x, 500)
                    img_h, img_w = self.bg_img_data.shape[:2]
                    self.bg_scale = w_sensors / max(img_w, 1)
                    self.bg_offset_x = (min_x + max_x) / 2 - (img_w * self.bg_scale) / 2
                    self.bg_offset_y = (min_y + max_y) / 2 + (img_h * self.bg_scale) / 2
                else:
                    self.bg_offset_x, self.bg_offset_y, self.bg_scale = 0.0, 0.0, 1.0

                self.is_edit_mode.set(True)
                self.reload_data_and_draw()
            except Exception as e:
                messagebox.showerror("Fehler", f"Bild konnte nicht geladen werden:\n{e}")

    def rotate_image(self, angle):
        if self.bg_img_original is None:
            return
        self.bg_rotation = (self.bg_rotation + angle) % 360
        self.apply_transformations()
        self.reload_data_and_draw()

    def flip_image(self, direction):
        if self.bg_img_original is None:
            return
        if direction == 'h':
            self.bg_flip_h = not self.bg_flip_h
        elif direction == 'v':
            self.bg_flip_v = not self.bg_flip_v
        self.apply_transformations()
        self.reload_data_and_draw()

    def apply_transformations(self):
        """Wendet Spiegelungen und Rotationen auf das NumPy Array an."""
        if self.bg_img_original is not None:
            img = self.bg_img_original.copy()

            if self.bg_flip_h:
                img = np.fliplr(img)
            if self.bg_flip_v:
                img = np.flipud(img)

            k = self.bg_rotation // 90
            self.bg_img_data = np.rot90(img, k=-k)

    def load_sensors(self):
        sensors = []
        for filepath in glob.glob(os.path.join(DATA_DIR, "*.json")):
            filename = os.path.basename(filepath)
            if filename.startswith("radio_") or filename in NON_SENSOR_FILES or filename.startswith("."):
                continue
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if "pos" in data and isinstance(data["pos"], list) and len(data["pos"]) == 2:
                        sensors.append({
                            "name": data.get("name", filename.replace(".json", "")),
                            "x": data["pos"][0],
                            "y": data["pos"][1]
                        })
            except Exception:
                pass
        return sensors

    def reload_data_and_draw(self):
        self.ax.clear()
        self.img_plot = None
        self.heatmap_plot = None
        has_heatmap = False
        heatmap_file = os.path.join(DATA_DIR, "radio_heatmap.json")

        # 1. BILD
        if self.bg_img_data is not None:
            self.img_plot = self.ax.imshow(self.bg_img_data, zorder=0, alpha=self.alpha_bg.get())
            self._apply_image_extent_without_draw()

        # 2. HEATMAP
        if os.path.exists(heatmap_file):
            try:
                with open(heatmap_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                grid = np.array(data["grid_data"]).T
                x_min, y_min = data["min_bounds_x"], data["min_bounds_y"]
                cs = data["cell_size_cm"]
                extent_heatmap = [x_min, x_min + (grid.shape[1] * cs), y_min, y_min + (grid.shape[0] * cs)]

                # VERWENDUNG DER EIGENEN FARBSKALA
                self.heatmap_plot = self.ax.imshow(
                    grid, origin='lower', cmap=trilola_cmap, extent=extent_heatmap,
                    alpha=self.alpha_heatmap.get(), interpolation='nearest', zorder=1, vmin=0.0
                )

                for cb in self.fig.axes:
                    if cb is not self.ax:
                        cb.remove()
                self.fig.colorbar(self.heatmap_plot, ax=self.ax, label='Dämpfung (dB/m)')
                has_heatmap = True
            except Exception:
                pass

        # 2b. GRUNDRISS
        self.draw_floorplan()

        # 3. SENSOREN
        sensors = self.load_sensors()
        texts = []
        for s in sensors:
            self.ax.scatter(s["x"], s["y"], c='cyan', s=100, edgecolors='black', zorder=2)
            txt = self.ax.text(s["x"], s["y"], s["name"], color='black',
                               bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="gray", alpha=0.8),
                               zorder=3)
            texts.append(txt)

        if HAS_ADJUST_TEXT and texts:
            adjust_text(texts, ax=self.ax, arrowprops=dict(arrowstyle='-', color='gray', lw=0.5))

        self.ax.set_title("TriLola RF-Tomographie & Tracker")
        self.ax.set_xlabel("X-Koordinate (cm)")
        self.ax.set_ylabel("Y-Koordinate (cm)")
        self.ax.grid(color='gray', linestyle='--', linewidth=0.5, alpha=0.3)

        if has_heatmap or sensors or self.bg_img_data is not None:
            self.ax.autoscale()

        self.canvas.draw_idle()
        self.update_info_labels()

    def _apply_image_extent_without_draw(self):
        if self.img_plot is None or self.bg_img_data is None:
            return
        img_h, img_w = self.bg_img_data.shape[:2]
        x_min = self.bg_offset_x
        x_max = self.bg_offset_x + (img_w * self.bg_scale)
        y_min = self.bg_offset_y - (img_h * self.bg_scale)
        y_max = self.bg_offset_y
        self.img_plot.set_extent((x_min, x_max, y_max, y_min))

    def update_image_extent(self):
        if self.img_plot is None or self.bg_img_data is None:
            return
        xlim, ylim = self.ax.get_xlim(), self.ax.get_ylim()
        self._apply_image_extent_without_draw()
        self.ax.set_xlim(xlim)
        self.ax.set_ylim(ylim)
        self.canvas.draw_idle()
        self.update_info_labels()

    # ==========================================
    #        MAUS-INTERAKTIONEN (Edit Mode)
    # ==========================================
    def on_scroll(self, event):
        if not self.is_edit_mode.get() or event.inaxes != self.ax or self.bg_img_data is None:
            return
        scale_factor = 1.05 if event.button == 'up' else (1 / 1.05)
        new_scale = self.bg_scale * scale_factor

        mouse_x, mouse_y = event.xdata, event.ydata
        img_x = (mouse_x - self.bg_offset_x) / self.bg_scale
        img_y = (mouse_y - self.bg_offset_y) / self.bg_scale

        self.bg_scale = new_scale
        self.bg_offset_x = mouse_x - (img_x * self.bg_scale)
        self.bg_offset_y = mouse_y - (img_y * self.bg_scale)

        self.update_image_extent()

    def on_press(self, event):
        if event.inaxes == self.ax and self.draw_mode.get() != "aus" and not self.toolbar.mode:
            self.handle_floorplan_click(event)
            return
        if not self.is_edit_mode.get() or event.inaxes != self.ax:
            return
        if event.button == 1:
            self.is_dragging = True
            self.last_mouse_x = event.xdata
            self.last_mouse_y = event.ydata

    def on_release(self, event):
        if event.button == 1:
            self.is_dragging = False

    def on_motion(self, event):
        if not self.is_dragging or not self.is_edit_mode.get() or event.inaxes != self.ax:
            return
        dx, dy = event.xdata - self.last_mouse_x, event.ydata - self.last_mouse_y
        self.bg_offset_x += dx
        self.bg_offset_y += dy
        self.last_mouse_x, self.last_mouse_y = event.xdata, event.ydata
        self.update_image_extent()

    # ==========================================
    #        GRUNDRISS-EDITOR
    # ==========================================
    def load_floorplan(self):
        try:
            with open(FLOORPLAN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("walls", [])
            data.setdefault("rooms", [])
            return data
        except (OSError, json.JSONDecodeError):
            return {"default_wall_db": 5.0, "walls": [], "rooms": []}

    def save_floorplan(self):
        try:
            self.floorplan["default_wall_db"] = float(self.wall_db.get())
            os.makedirs(os.path.dirname(FLOORPLAN_FILE), exist_ok=True)
            tmp = FLOORPLAN_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.floorplan, f, indent=2, ensure_ascii=False)
            os.replace(tmp, FLOORPLAN_FILE)
            messagebox.showinfo("Gespeichert", f"Grundriss gespeichert:\n{FLOORPLAN_FILE}\n\n"
                                "Tracker neu starten, damit er übernommen wird.")
        except Exception as e:
            messagebox.showerror("Fehler", f"Speichern fehlgeschlagen:\n{e}")

    def on_draw_mode_changed(self):
        self.pending_points = []
        if self.draw_mode.get() != "aus":
            self.is_edit_mode.set(False)
            if self.toolbar.mode:
                self.toolbar.pan() if self.toolbar.mode == "pan/zoom" else self.toolbar.zoom()
        self.reload_data_and_draw()

    def _snap(self, x, y):
        best, best_d = (x, y), 15.0
        for wall in self.floorplan["walls"]:
            for px, py in (wall["a"], wall["b"]):
                d = float(np.hypot(px - x, py - y))
                if d < best_d:
                    best, best_d = (px, py), d
        for room in self.floorplan["rooms"]:
            for px, py in room["polygon"]:
                d = float(np.hypot(px - x, py - y))
                if d < best_d:
                    best, best_d = (px, py), d
        return round(float(best[0]), 1), round(float(best[1]), 1)

    def handle_floorplan_click(self, event):
        x, y = self._snap(event.xdata, event.ydata)
        mode = self.draw_mode.get()
        if mode == "wand":
            self.pending_points.append([x, y])
            if len(self.pending_points) == 2:
                a, b = self.pending_points
                if a != b:
                    try:
                        db = float(self.wall_db.get())
                    except (tk.TclError, ValueError):
                        db = 5.0
                    self.floorplan["walls"].append({"a": a, "b": b, "attenuation_db": db, "blocking": True})
                    self.history.append(("walls", len(self.floorplan["walls"]) - 1))
                self.pending_points = []
        elif mode == "raum":
            if event.button == 3:
                self.finish_room()
                return
            self.pending_points.append([x, y])
        self.reload_data_and_draw()

    def finish_room(self):
        if self.draw_mode.get() != "raum" or len(self.pending_points) < 3:
            messagebox.showinfo("Raum", "Mindestens 3 Ecken im Modus „Raum zeichnen“ klicken.")
            return
        name = simpledialog.askstring("Raumname", "Name des Raums:", parent=self)
        if name:
            self.floorplan["rooms"].append({"name": name.strip(), "polygon": self.pending_points})
            self.history.append(("rooms", len(self.floorplan["rooms"]) - 1))
        self.pending_points = []
        self.reload_data_and_draw()

    def undo_floorplan(self):
        if self.pending_points:
            self.pending_points.pop()
        elif self.history:
            kind, index = self.history.pop()
            if index < len(self.floorplan[kind]):
                self.floorplan[kind].pop(index)
        elif self.floorplan["walls"]:
            self.floorplan["walls"].pop()
        self.reload_data_and_draw()

    def draw_floorplan(self):
        scale = float(self.floorplan.get("wall_scale", 1.0) or 1.0)
        for room in self.floorplan.get("rooms", []):
            poly = np.asarray(room["polygon"], dtype=float)
            if len(poly) < 3:
                continue
            self.ax.fill(poly[:, 0], poly[:, 1], alpha=0.12, color="tab:blue", zorder=1.5)
            cx, cy = poly.mean(axis=0)
            self.ax.text(cx, cy, room.get("name", ""), ha="center", va="center", color="tab:blue",
                         fontsize=10, alpha=0.8, zorder=1.6)
        for wall in self.floorplan.get("walls", []):
            (x1, y1), (x2, y2) = wall["a"], wall["b"]
            db = float(wall.get("attenuation_db", self.floorplan.get("default_wall_db", 5.0))) * scale
            self.ax.plot([x1, x2], [y1, y2], color="black", linewidth=1.0 + db / 3.0, zorder=1.7,
                         solid_capstyle="butt")
        if self.pending_points:
            pts = np.asarray(self.pending_points, dtype=float)
            self.ax.plot(pts[:, 0], pts[:, 1], "o--", color="tab:orange", zorder=4)


if __name__ == "__main__":
    app = TriLolaViewerApp()
    app.mainloop()