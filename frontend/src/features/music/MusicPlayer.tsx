import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";
import { ListMusic, Pause, Play, SkipBack, SkipForward, Volume2 } from "lucide-react";
import { formatDuration, getMusic } from "../../lib/api";
import { cn } from "../../lib/cn";
import { currentTrack, usePlayerStore } from "./playerStore";

export function MusicPlayer() {
  const audioRef = useRef<HTMLAudioElement>(null);
  const location = useLocation();
  const inReader = location.pathname.startsWith("/read");
  const [position, setPosition] = useState(0);
  const [duration, setDuration] = useState(0);
  const track = usePlayerStore(currentTrack);
  const playing = usePlayerStore((state) => state.playing);
  const volume = usePlayerStore((state) => state.volume);
  const loop = usePlayerStore((state) => state.loop);
  const setTracks = usePlayerStore((state) => state.setTracks);
  const musicQuery = useQuery({
    queryKey: ["music"],
    queryFn: getMusic,
    staleTime: 60_000,
  });
  const toggle = usePlayerStore((state) => state.toggle);
  const next = usePlayerStore((state) => state.next);
  const prev = usePlayerStore((state) => state.prev);
  const setPlaying = usePlayerStore((state) => state.setPlaying);
  const setVolume = usePlayerStore((state) => state.setVolume);
  const setLoop = usePlayerStore((state) => state.setLoop);

  useEffect(() => {
    if (musicQuery.data?.tracks) setTracks(musicQuery.data.tracks);
  }, [musicQuery.data?.tracks, setTracks]);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    if (!track) {
      audio.pause();
      audio.removeAttribute("src");
      delete audio.dataset.trackId;
      audio.load();
      setPosition(0);
      setDuration(0);
      return;
    }
    if (audio.dataset.trackId !== track.id) {
      audio.dataset.trackId = track.id;
      audio.src = track.url;
      audio.load();
      setPosition(0);
      setDuration(0);
    }
  }, [track]);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio || !track) return;
    if (playing) {
      void audio.play().catch(() => setPlaying(false));
    } else {
      audio.pause();
    }
  }, [playing, track, setPlaying]);

  useEffect(() => {
    if (audioRef.current) audioRef.current.volume = volume;
  }, [volume]);

  useEffect(() => {
    if (audioRef.current) audioRef.current.loop = loop;
  }, [loop]);

  useEffect(() => {
    if (!("mediaSession" in navigator)) return;
    if (!track) {
      navigator.mediaSession.metadata = null;
      return;
    }
    navigator.mediaSession.metadata = new MediaMetadata({
      title: track.title,
      artist: track.artist || "Nhạc nền",
      album: "Translate Book",
    });
    navigator.mediaSession.setActionHandler("play", () => setPlaying(true));
    navigator.mediaSession.setActionHandler("pause", () => setPlaying(false));
    navigator.mediaSession.setActionHandler("nexttrack", next);
    navigator.mediaSession.setActionHandler("previoustrack", prev);
    return () => {
      navigator.mediaSession.setActionHandler("play", null);
      navigator.mediaSession.setActionHandler("pause", null);
      navigator.mediaSession.setActionHandler("nexttrack", null);
      navigator.mediaSession.setActionHandler("previoustrack", null);
    };
  }, [track, next, prev, setPlaying]);

  const progress = duration > 0 ? Math.min(100, (position / duration) * 100) : 0;

  return (
    <>
      <audio
        ref={audioRef}
        preload="metadata"
        playsInline
        onTimeUpdate={(event) => setPosition(event.currentTarget.currentTime)}
        onLoadedMetadata={(event) => setDuration(event.currentTarget.duration || 0)}
        onPlay={() => setPlaying(true)}
        onEnded={() => {
          if (!loop) next();
        }}
      />

      {track ? (
        <div
          className={cn(
            "fixed inset-x-3 z-40 mx-auto max-w-5xl rounded-2xl border border-line bg-surface/95 p-3 shadow-2xl shadow-black/30 backdrop-blur",
            inReader
              ? "bottom-[calc(0.75rem+env(safe-area-inset-bottom))] md:inset-x-4 md:bottom-4 md:max-w-none"
              : "bottom-[calc(4.75rem+env(safe-area-inset-bottom))] md:inset-x-auto md:bottom-5 md:left-[17.5rem] md:right-6 md:max-w-none",
          )}
        >
          <div className="flex min-w-0 items-center gap-3">
            <Link
              to="/music"
              className="focus-ring flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-accent/80 to-cyan-700 text-accent-ink"
              aria-label="Mở nhạc nền"
            >
              <ListMusic className="h-5 w-5" />
            </Link>

            <div className="min-w-0 flex-1">
              <Link to="/music" className="block truncate text-sm font-semibold text-ink hover:underline">
                {track.title}
              </Link>
              <div className="truncate text-xs text-muted">
                {track.artist || "Nhạc nền"} · {formatDuration(track.duration || duration)}
              </div>
            </div>

            <div className="hidden items-center gap-1 sm:flex">
              <button className="focus-ring rounded-lg p-2 text-muted hover:bg-surface-2 hover:text-ink" onClick={prev} aria-label="Bài trước">
                <SkipBack className="h-4 w-4" />
              </button>
              <button
                className="focus-ring flex h-10 w-10 items-center justify-center rounded-full bg-accent text-accent-ink hover:brightness-110"
                onClick={toggle}
                aria-label={playing ? "Tạm dừng" : "Phát"}
              >
                {playing ? <Pause className="h-4 w-4" /> : <Play className="ml-0.5 h-4 w-4" />}
              </button>
              <button className="focus-ring rounded-lg p-2 text-muted hover:bg-surface-2 hover:text-ink" onClick={next} aria-label="Bài tiếp">
                <SkipForward className="h-4 w-4" />
              </button>
            </div>

            <button
              className="focus-ring flex h-10 w-10 items-center justify-center rounded-full bg-accent text-accent-ink sm:hidden"
              onClick={toggle}
              aria-label={playing ? "Tạm dừng" : "Phát"}
            >
              {playing ? <Pause className="h-4 w-4" /> : <Play className="ml-0.5 h-4 w-4" />}
            </button>

            <div className="hidden items-center gap-2 md:flex">
              <Volume2 className="h-4 w-4 text-faint" />
              <input
                className="h-1 w-24 accent-[var(--color-accent)]"
                type="range"
                min="0"
                max="100"
                value={Math.round(volume * 100)}
                onChange={(event) => setVolume(Number(event.target.value) / 100)}
                aria-label="Âm lượng nhạc"
              />
            </div>
            <button
              className={cn(
                "focus-ring hidden rounded-lg px-2 py-1 text-xs font-semibold md:block",
                loop ? "bg-accent-soft text-accent" : "text-faint hover:text-ink",
              )}
              onClick={() => setLoop(!loop)}
              aria-pressed={loop}
              title="Lặp lại"
            >
              LOOP
            </button>
          </div>

          <div className="mt-2 h-0.5 overflow-hidden rounded-full bg-surface-3">
            <div className="h-full bg-accent transition-[width] duration-500" style={{ width: `${progress}%` }} />
          </div>
        </div>
      ) : null}
    </>
  );
}
