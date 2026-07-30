import {
  PointerEvent as ReactPointerEvent,
  useEffect,
  useLayoutEffect,
  useRef,
  useState
} from "react";

type PetEdge = "auto" | "left" | "right";
type PetMood = "idle" | "peek" | "happy" | "sleepy";
type PetId = "yuexinmiao1" | "trump" | "doro" | "guga" | "yuexinmiao" | "ikkun";
type PetAction =
  | "idle" | "walk" | "run" | "jump" | "roll" | "stretch" | "sleep" | "click" | "eat" | "peek" | "drag"
  | "happy" | "angry" | "sad" | "surprised" | "curious" | "sleepy";

type PetPreferences = {
  motion: boolean;
  size: number;
  opacity: number;
  edge: PetEdge;
  petId: PetId;
};

type PetSpot = {
  x: number;
  y: number;
  mood: PetMood;
  from: "left" | "right" | "bottom";
};

type SpriteFrame = {
  column: number;
  row: number;
};

type PetSequence = {
  fps: number;
  frames: SpriteFrame[];
};

const PET_SETTINGS_KEY = "k-agent-desktop-pet-settings";
const PET_VIEWPORT_GUTTER = 24;
const PET_MENU_WIDTH = 278;
const PETS: Array<{
  id: PetId;
  displayName: string;
  menuLabel: string;
  description: string;
  spritesheet: string;
}> = [
  {
    id: "yuexinmiao1",
    displayName: "月薪喵",
    menuLabel: "月薪喵·经典",
    description: "贴纸风办公室小猫",
    spritesheet: "/desktop-pet/yuexinmiao1/spritesheet.webp"
  },
  {
    id: "trump",
    displayName: "Trump",
    menuLabel: "Trump",
    description: "金发人物桌宠",
    spritesheet: "/desktop-pet/trump/spritesheet.webp"
  },
  {
    id: "doro",
    displayName: "Doro",
    menuLabel: "Doro",
    description: "粉发白色小精灵",
    spritesheet: "/desktop-pet/doro/spritesheet.webp"
  },
  {
    id: "guga",
    displayName: "咕嘎",
    menuLabel: "咕嘎",
    description: "企鹅帽女孩桌宠",
    spritesheet: "/desktop-pet/guga/spritesheet.webp"
  },
  {
    id: "yuexinmiao",
    displayName: "月薪喵",
    menuLabel: "月薪喵·新版",
    description: "新版白色办公室小猫",
    spritesheet: "/desktop-pet/yuexinmiao/spritesheet.webp"
  },
  {
    id: "ikkun",
    displayName: "ikkun",
    menuLabel: "ikkun",
    description: "团雀风人物桌宠",
    spritesheet: "/desktop-pet/ikkun/spritesheet.webp"
  }
];
const DEFAULT_PREFERENCES: PetPreferences = {
  motion: true,
  size: 118,
  opacity: 0.96,
  edge: "auto",
  petId: "yuexinmiao1"
};
const frameRow = (row: number, count: number): SpriteFrame[] => (
  Array.from({ length: count }, (_, column) => ({ column, row }))
);
const PET_SEQUENCES: Record<PetAction, PetSequence> = {
  idle: { fps: 3, frames: frameRow(0, 6) },
  walk: { fps: 9, frames: frameRow(1, 8) },
  run: { fps: 12, frames: frameRow(2, 8) },
  jump: { fps: 10, frames: frameRow(4, 5) },
  roll: { fps: 8, frames: frameRow(4, 5) },
  stretch: { fps: 9, frames: frameRow(4, 5) },
  sleep: { fps: 4, frames: frameRow(5, 8) },
  click: { fps: 7, frames: frameRow(3, 4) },
  eat: { fps: 8, frames: frameRow(4, 5) },
  peek: { fps: 6, frames: frameRow(6, 6) },
  drag: { fps: 3, frames: frameRow(0, 6) },
  happy: { fps: 7, frames: frameRow(3, 4) },
  angry: { fps: 9, frames: frameRow(8, 6) },
  sad: { fps: 7, frames: frameRow(5, 8) },
  surprised: { fps: 10, frames: frameRow(4, 5) },
  curious: { fps: 6, frames: frameRow(6, 6) },
  sleepy: { fps: 4, frames: frameRow(8, 6) }
};
const AMBIENT_ACTIONS: Array<{ action: PetAction; duration: number }> = [
  { action: "happy", duration: 1800 },
  { action: "curious", duration: 2200 },
  { action: "stretch", duration: 1800 },
  { action: "jump", duration: 1300 },
  { action: "peek", duration: 2400 },
  { action: "sleep", duration: 3200 },
  { action: "idle", duration: 1800 }
];

function readPreferences(): PetPreferences {
  try {
    const saved = JSON.parse(localStorage.getItem(PET_SETTINGS_KEY) ?? "{}") as Partial<PetPreferences>;
    return {
      motion: typeof saved.motion === "boolean" ? saved.motion : DEFAULT_PREFERENCES.motion,
      size: typeof saved.size === "number" ? Math.min(168, Math.max(82, saved.size)) : DEFAULT_PREFERENCES.size,
      opacity: typeof saved.opacity === "number" ? Math.min(1, Math.max(0.45, saved.opacity)) : DEFAULT_PREFERENCES.opacity,
      edge: saved.edge === "left" || saved.edge === "right" || saved.edge === "auto"
        ? saved.edge
        : DEFAULT_PREFERENCES.edge,
      petId: PETS.some((pet) => pet.id === saved.petId)
        ? saved.petId as PetId
        : DEFAULT_PREFERENCES.petId
    };
  } catch {
    return DEFAULT_PREFERENCES;
  }
}

function clamp(value: number, minimum: number, maximum: number) {
  return Math.min(maximum, Math.max(minimum, value));
}

function nextPetSpot(size: number, edge: PetEdge): PetSpot {
  const selectors = [
    ".sidebar",
    ".inspector",
    ".composer",
    ".config-nav",
    ".config-section"
  ];
  const rectangles = selectors
    .flatMap((selector) => Array.from(document.querySelectorAll<HTMLElement>(selector)))
    .map((element) => element.getBoundingClientRect())
    .filter((rect) => rect.width > 80 && rect.height > 80 && rect.right > 0 && rect.left < window.innerWidth);
  const moods: PetMood[] = ["peek", "idle", "happy", "peek", "sleepy"];
  const mood = moods[Math.floor(Math.random() * moods.length)];

  if (rectangles.length && edge === "auto" && Math.random() > 0.3) {
    const rect = rectangles[Math.floor(Math.random() * rectangles.length)];
    const useVerticalEdge = rect.height > size * 1.5 && Math.random() > 0.42;
    if (useVerticalEdge) {
      const from = rect.left > window.innerWidth / 2 ? "right" : "left";
      const x = from === "right" ? rect.left - size * 0.62 : rect.right - size * 0.38;
      return {
        x: clamp(x, PET_VIEWPORT_GUTTER, window.innerWidth - size - PET_VIEWPORT_GUTTER),
        y: clamp(rect.top + rect.height * (0.2 + Math.random() * 0.55), 64, window.innerHeight - size - PET_VIEWPORT_GUTTER),
        mood: "peek",
        from
      };
    }
    return {
      x: clamp(rect.right - size - 20, PET_VIEWPORT_GUTTER, window.innerWidth - size - PET_VIEWPORT_GUTTER),
      y: clamp(rect.top - size * 0.52, 64, window.innerHeight - size - PET_VIEWPORT_GUTTER),
      mood,
      from: "bottom"
    };
  }

  const from = edge === "auto" ? (Math.random() > 0.5 ? "right" : "left") : edge;
  return {
    x: from === "right" ? window.innerWidth - size - PET_VIEWPORT_GUTTER : PET_VIEWPORT_GUTTER,
    y: clamp(window.innerHeight - size - PET_VIEWPORT_GUTTER - Math.random() * Math.min(190, window.innerHeight * 0.3), 64, window.innerHeight - size - PET_VIEWPORT_GUTTER),
    mood,
    from
  };
}

export function DesktopPet({
  enabled,
  onEnabledChange
}: {
  enabled: boolean;
  onEnabledChange: (enabled: boolean) => void;
}) {
  const [preferences, setPreferences] = useState<PetPreferences>(readPreferences);
  const [spot, setSpot] = useState<PetSpot>(() => ({
    x: Math.max(12, window.innerWidth - DEFAULT_PREFERENCES.size - 30),
    y: Math.max(80, window.innerHeight - DEFAULT_PREFERENCES.size - 28),
    mood: "idle",
    from: "bottom"
  }));
  const [menuOpen, setMenuOpen] = useState(false);
  const [action, setAction] = useState<PetAction>("idle");
  const [spriteFrameIndex, setSpriteFrameIndex] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [menuOffset, setMenuOffset] = useState({ left: 0, top: 0 });
  const [facing, setFacing] = useState<"left" | "right">("right");
  const layerRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const spotRef = useRef(spot);
  const dragRef = useRef<{ startX: number; startY: number; originX: number; originY: number; moved: boolean } | null>(null);
  const suppressClickRef = useRef(false);
  const pendingArrivalRef = useRef<PetSpot | null>(null);
  const movementTimerRef = useRef<number | null>(null);
  const actionTimerRef = useRef<number | null>(null);

  useEffect(() => {
    localStorage.setItem(PET_SETTINGS_KEY, JSON.stringify(preferences));
  }, [preferences]);

  useEffect(() => {
    spotRef.current = spot;
  }, [spot]);

  useEffect(() => {
    const reposition = () => setSpot((current) => ({
      ...current,
      x: clamp(current.x, PET_VIEWPORT_GUTTER, window.innerWidth - preferences.size - PET_VIEWPORT_GUTTER),
      y: clamp(current.y, 64, window.innerHeight - preferences.size - PET_VIEWPORT_GUTTER)
    }));
    window.addEventListener("resize", reposition);
    return () => window.removeEventListener("resize", reposition);
  }, [preferences.size]);

  useEffect(() => {
    if (!enabled || !preferences.motion || menuOpen || dragging) return;
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduceMotion) return;
    let timeout = 0;
    const performRoutine = () => {
      if (Math.random() < 0.45) {
        moveTo(nextPetSpot(preferences.size, preferences.edge));
      } else {
        playAmbientAction();
      }
      timeout = window.setTimeout(performRoutine, 8000 + Math.random() * 4000);
    };
    timeout = window.setTimeout(performRoutine, 1800);
    return () => window.clearTimeout(timeout);
  }, [dragging, enabled, menuOpen, preferences.edge, preferences.motion, preferences.size]);

  useEffect(() => {
    setSpriteFrameIndex(0);
    if (!enabled || !preferences.motion) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const sequence = PET_SEQUENCES[action];
    if (sequence.frames.length < 2) return;
    const timer = window.setInterval(() => {
      setSpriteFrameIndex((current) => (current + 1) % sequence.frames.length);
    }, 1000 / sequence.fps);
    return () => window.clearInterval(timer);
  }, [action, enabled, preferences.motion]);

  useEffect(() => {
    if (preferences.motion) return;
    pendingArrivalRef.current = null;
    if (movementTimerRef.current !== null) {
      window.clearTimeout(movementTimerRef.current);
      movementTimerRef.current = null;
    }
    if (actionTimerRef.current !== null) {
      window.clearTimeout(actionTimerRef.current);
      actionTimerRef.current = null;
    }
    setAction("idle");
  }, [preferences.motion]);

  useEffect(() => () => {
    if (movementTimerRef.current !== null) window.clearTimeout(movementTimerRef.current);
    if (actionTimerRef.current !== null) window.clearTimeout(actionTimerRef.current);
  }, []);

  useEffect(() => {
    if (!menuOpen) return;
    const close = (event: globalThis.PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [menuOpen]);

  useLayoutEffect(() => {
    if (!menuOpen || !menuRef.current) return;
    const rect = menuRef.current.getBoundingClientRect();
    const safeEdge = 12;
    let deltaX = 0;
    let deltaY = 0;
    if (rect.left < safeEdge) deltaX = safeEdge - rect.left;
    if (rect.right > window.innerWidth - safeEdge) deltaX = window.innerWidth - safeEdge - rect.right;
    if (rect.top < safeEdge) deltaY = safeEdge - rect.top;
    if (rect.bottom > window.innerHeight - safeEdge) deltaY = window.innerHeight - safeEdge - rect.bottom;
    if (Math.abs(deltaX) > 0.5 || Math.abs(deltaY) > 0.5) {
      setMenuOffset((current) => ({
        left: current.left + deltaX,
        top: current.top + deltaY
      }));
    }
  }, [menuOpen, menuOffset.left, menuOffset.top, preferences.size, spot.x, spot.y]);

  if (!enabled) return null;

  const activePet = PETS.find((pet) => pet.id === preferences.petId) ?? PETS[0];
  const sequence = PET_SEQUENCES[action];
  const spriteFrame = sequence.frames[spriteFrameIndex % sequence.frames.length];

  const updatePreference = <Key extends keyof PetPreferences>(key: Key, value: PetPreferences[Key]) => {
    setPreferences((current) => ({ ...current, [key]: value }));
  };

  function playAction(nextAction: PetAction, duration = 1000) {
    if (actionTimerRef.current !== null) window.clearTimeout(actionTimerRef.current);
    setAction(nextAction);
    if (nextAction === "idle") return;
    actionTimerRef.current = window.setTimeout(() => {
      setAction("idle");
      actionTimerRef.current = null;
    }, duration);
  }

  function playAmbientAction(mood?: PetMood) {
    const selected = mood === "peek"
      ? { action: "peek" as PetAction, duration: 2400 }
      : mood === "sleepy"
        ? { action: "sleep" as PetAction, duration: 3200 }
        : mood === "happy"
          ? { action: "happy" as PetAction, duration: 1800 }
          : AMBIENT_ACTIONS[Math.floor(Math.random() * AMBIENT_ACTIONS.length)];
    playAction(selected.action, selected.duration);
  }

  function finishTravel() {
    const arrival = pendingArrivalRef.current;
    if (!arrival) return;
    pendingArrivalRef.current = null;
    if (movementTimerRef.current !== null) {
      window.clearTimeout(movementTimerRef.current);
      movementTimerRef.current = null;
    }
    playAmbientAction(arrival.mood);
  }

  function moveTo(nextSpot: PetSpot) {
    const current = spotRef.current;
    const distance = Math.hypot(nextSpot.x - current.x, nextSpot.y - current.y);
    if (distance < 48) {
      playAmbientAction(nextSpot.mood);
      return;
    }
    if (actionTimerRef.current !== null) {
      window.clearTimeout(actionTimerRef.current);
      actionTimerRef.current = null;
    }
    if (movementTimerRef.current !== null) window.clearTimeout(movementTimerRef.current);
    pendingArrivalRef.current = nextSpot;
    setFacing(nextSpot.x < current.x ? "left" : "right");
    // Autonomous travel is deliberately slow, so walking is the only matching locomotion animation.
    setAction("walk");
    setSpot(nextSpot);
    movementTimerRef.current = window.setTimeout(finishTravel, 4100);
  }

  const handlePointerDown = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return;
    if (movementTimerRef.current !== null) {
      window.clearTimeout(movementTimerRef.current);
      movementTimerRef.current = null;
    }
    pendingArrivalRef.current = null;
    if (actionTimerRef.current !== null) {
      window.clearTimeout(actionTimerRef.current);
      actionTimerRef.current = null;
    }
    const layerRect = layerRef.current?.getBoundingClientRect();
    const originX = layerRect?.left ?? spot.x;
    const originY = layerRect?.top ?? spot.y;
    event.currentTarget.setPointerCapture(event.pointerId);
    setDragging(true);
    // Capture the rendered position so grabbing a pet during autonomous travel never makes it jump to the pending destination.
    setSpot((current) => ({
      ...current,
      x: originX,
      y: originY
    }));
    dragRef.current = {
      startX: event.clientX,
      startY: event.clientY,
      originX,
      originY,
      moved: false
    };
  };

  const handlePointerMove = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (!dragRef.current) return;
    const drag = dragRef.current;
    if (Math.abs(event.clientX - drag.startX) + Math.abs(event.clientY - drag.startY) > 4) {
      drag.moved = true;
      setAction("drag");
      setFacing(event.clientX < drag.startX ? "left" : "right");
    }
    setSpot((current) => ({
      ...current,
      x: clamp(drag.originX + event.clientX - drag.startX, PET_VIEWPORT_GUTTER, window.innerWidth - preferences.size - PET_VIEWPORT_GUTTER),
      y: clamp(drag.originY + event.clientY - drag.startY, 64, window.innerHeight - preferences.size - PET_VIEWPORT_GUTTER),
      mood: "idle"
    }));
  };

  const finishDrag = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const moved = dragRef.current?.moved ?? false;
    setDragging(false);
    dragRef.current = null;
    if (moved && event.type !== "pointercancel") {
      suppressClickRef.current = true;
      playAction("click");
    } else if (action === "drag") {
      setAction("idle");
    }
  };

  return (
    <div
      ref={layerRef}
      className={`desktop-pet-layer pet-from-${spot.from} pet-facing-${facing} ${dragging ? "pet-is-dragging" : ""}`}
      onTransitionEnd={(event) => {
        if (event.target === event.currentTarget && event.propertyName === "transform") finishTravel();
      }}
      style={{
        "--pet-x": `${spot.x}px`,
        "--pet-y": `${spot.y}px`,
        "--pet-size": `${preferences.size}px`,
        "--pet-opacity": preferences.opacity
      } as React.CSSProperties}
    >
      <button
        className={`desktop-pet pet-action-${action} ${preferences.motion ? "pet-motion-enabled" : ""}`}
        data-action={action}
        type="button"
        aria-label={`桌宠${activePet.displayName}，点击互动，右键打开设置`}
        title="拖动我，或右键打开桌宠设置"
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={finishDrag}
        onPointerCancel={finishDrag}
        onClick={() => {
          if (suppressClickRef.current) {
            suppressClickRef.current = false;
            return;
          }
          playAction("click");
        }}
        onDoubleClick={() => playAction("roll")}
        onContextMenu={(event) => {
          event.preventDefault();
          setMenuOffset({
            left: spot.x + preferences.size / 2 > window.innerWidth / 2
              ? 18 - PET_MENU_WIDTH
              : preferences.size - 18,
            top: 0
          });
          setMenuOpen(true);
        }}
      >
        <span className="desktop-pet-glow" />
        <span className="desktop-pet-frames" aria-hidden="true">
          <span
            className="pet-sprite"
            style={{
              backgroundImage: `url(${activePet.spritesheet})`,
              backgroundPosition: `${(spriteFrame.column / 7) * 100}% ${(spriteFrame.row / 8) * 100}%`
            }}
          />
        </span>
        <span className="desktop-pet-shadow" />
        <span className="desktop-pet-heart" aria-hidden="true">♥</span>
        {(action === "sleep" || action === "sleepy") && <span className="desktop-pet-z" aria-hidden="true">zZ</span>}
      </button>

      {menuOpen && (
        <div
          ref={menuRef}
          className="desktop-pet-menu"
          role="dialog"
          aria-label="桌宠设置"
          style={{ left: menuOffset.left, top: menuOffset.top }}
        >
          <header>
            <span className="pet-menu-avatar">
              <i
                className="pet-menu-avatar-sprite"
                style={{ backgroundImage: `url(${activePet.spritesheet})` }}
              />
            </span>
            <span><strong>{activePet.displayName}</strong><small>{activePet.description}</small></span>
            <button type="button" onClick={() => setMenuOpen(false)} aria-label="关闭桌宠设置">×</button>
          </header>
          <fieldset className="pet-menu-picker">
            <legend>更换桌宠</legend>
            {PETS.map((pet) => (
              <button
                key={pet.id}
                className={preferences.petId === pet.id ? "active" : ""}
                type="button"
                aria-pressed={preferences.petId === pet.id}
                title={pet.description}
                onClick={() => {
                  updatePreference("petId", pet.id);
                  setSpriteFrameIndex(0);
                  playAction("happy", 1100);
                }}
              >
                <i style={{ backgroundImage: `url(${pet.spritesheet})` }} />
                <span>{pet.menuLabel}</span>
              </button>
            ))}
          </fieldset>
          <label className="pet-menu-row">
            <span><strong>开启桌宠</strong><small>在所有页面显示</small></span>
            <input type="checkbox" checked={enabled} onChange={(event) => onEnabledChange(event.target.checked)} />
            <i />
          </label>
          <label className="pet-menu-row">
            <span><strong>自主动作</strong><small>探头、出现和休息</small></span>
            <input type="checkbox" checked={preferences.motion} onChange={(event) => updatePreference("motion", event.target.checked)} />
            <i />
          </label>
          <label className="pet-menu-range">
            <span><strong>大小</strong><b>{preferences.size}px</b></span>
            <input type="range" min="82" max="168" value={preferences.size} onChange={(event) => updatePreference("size", Number(event.target.value))} />
          </label>
          <label className="pet-menu-range">
            <span><strong>透明度</strong><b>{Math.round(preferences.opacity * 100)}%</b></span>
            <input type="range" min="45" max="100" value={Math.round(preferences.opacity * 100)} onChange={(event) => updatePreference("opacity", Number(event.target.value) / 100)} />
          </label>
          <fieldset className="pet-menu-position">
            <legend>常驻位置</legend>
            {([
              ["auto", "自动"],
              ["left", "左侧"],
              ["right", "右侧"]
            ] as Array<[PetEdge, string]>).map(([value, label]) => (
              <button
                key={value}
                className={preferences.edge === value ? "active" : ""}
                type="button"
                onClick={() => {
                  updatePreference("edge", value);
                  setMenuOpen(false);
                  moveTo(nextPetSpot(preferences.size, value));
                }}
              >
                {label}
              </button>
            ))}
          </fieldset>
          <p>可拖动桌宠改变临时位置 · 右键再次打开设置</p>
        </div>
      )}
    </div>
  );
}
