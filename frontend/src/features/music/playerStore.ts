import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { Track } from "../../lib/api";

type PlayerState = {
  tracks: Track[];
  currentId: string | null;
  playing: boolean;
  volume: number;
  loop: boolean;
  setTracks: (tracks: Track[]) => void;
  playTrack: (track: Track) => void;
  toggle: () => void;
  pause: () => void;
  setPlaying: (playing: boolean) => void;
  next: () => void;
  prev: () => void;
  setVolume: (volume: number) => void;
  setLoop: (loop: boolean) => void;
  removeTrack: (id: string) => void;
};

export const usePlayerStore = create<PlayerState>()(
  persist(
    (set, get) => ({
      tracks: [],
      currentId: null,
      playing: false,
      volume: 0.28,
      loop: true,

      setTracks: (tracks) => {
        const { currentId } = get();
        const exists = currentId && tracks.some((track) => track.id === currentId);
        set({
          tracks,
          // Never auto-select the first track: a global mini-player should only
          // appear after the user has actually chosen something.
          currentId: exists ? currentId : null,
        });
      },

      playTrack: (track) => set({ currentId: track.id, playing: true }),

      toggle: () => {
        const { currentId, tracks, playing } = get();
        if (!currentId && tracks[0]) {
          set({ currentId: tracks[0].id, playing: true });
          return;
        }
        set({ playing: !playing });
      },

      pause: () => set({ playing: false }),
      setPlaying: (playing) => set({ playing }),

      next: () => {
        const { tracks, currentId } = get();
        if (!tracks.length) return;
        const index = Math.max(0, tracks.findIndex((track) => track.id === currentId));
        const nextTrack = tracks[(index + 1) % tracks.length];
        set({ currentId: nextTrack.id, playing: true });
      },

      prev: () => {
        const { tracks, currentId } = get();
        if (!tracks.length) return;
        const index = Math.max(0, tracks.findIndex((track) => track.id === currentId));
        const prevTrack = tracks[(index - 1 + tracks.length) % tracks.length];
        set({ currentId: prevTrack.id, playing: true });
      },

      setVolume: (volume) => set({ volume: Math.min(1, Math.max(0, volume)) }),
      setLoop: (loop) => set({ loop }),

      removeTrack: (id) => {
        const { tracks, currentId } = get();
        const index = tracks.findIndex((track) => track.id === id);
        const remaining = tracks.filter((track) => track.id !== id);
        if (currentId !== id) {
          set({ tracks: remaining });
          return;
        }
        const fallback = remaining[Math.min(index, Math.max(0, remaining.length - 1))] ?? null;
        set({
          tracks: remaining,
          currentId: fallback?.id ?? null,
          playing: false,
        });
      },
    }),
    {
      name: "v2.bgm",
      partialize: (state) => ({
        currentId: state.currentId,
        volume: state.volume,
        loop: state.loop,
      }),
    },
  ),
);

export function currentTrack(state: PlayerState) {
  return state.tracks.find((track) => track.id === state.currentId) ?? null;
}
