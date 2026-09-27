//! The engine's stderr, kept where a window user can actually see it.
//!
//! The launcher used to hand the engine's stderr straight to this process's
//! stderr (`Stdio::inherit`), which is right for a person with a terminal in
//! front of them and useless for the person using the app: the one diagnostic
//! that matters is the engine's last word before it died, and it went to a log
//! file or a console nobody is looking at. The bounded-shutdown backstop says
//! so out loud — `[engine] shutdown unfinished after 6s — exiting anyway` —
//! and then `os._exit`s, so that line is the entire explanation of an engine
//! that disappeared, thrown away by the same pipe that carried it.
//!
//! So the pipe is read here instead, and every completed line is both kept and
//! echoed. Echoing is not a second copy of the log: the terminal shows exactly
//! the bytes it showed when this was `Stdio::inherit`, and the app can ask for
//! the same lines afterwards. What is new is only that they are still there.
//!
//! Bounded twice over, because this buffer is fed by another process:
//!
//! * [`CAPACITY`] lines, oldest dropped. A long session's stderr must not grow
//!   into memory the app is supposed to be small in.
//! * [`MAX_LINE_CHARS`] per line, and [`MAX_PENDING_BYTES`] of unterminated
//!   bytes. A process printing a megabyte without a newline would otherwise
//!   park it in `partial` until the read returned, which is the unbounded case
//!   a ring buffer of lines does not cover on its own.
//!
//! Bytes are decoded only once a line is complete, never per chunk: a
//! multi-byte character split across two reads decodes to two replacement
//! characters if each half is decoded alone, and engine output is UTF-8 that a
//! shell person will read.

use std::collections::VecDeque;
use std::sync::Mutex;

/// Lines kept. Enough to cover a boot and a crash, small enough to be a detail
/// panel rather than a log viewer.
pub const CAPACITY: usize = 200;

/// How much of one line is kept, in characters. A traceback line with a giant
/// embedded payload is truncated with a visible ellipsis rather than dropped:
/// the point is that something was said, and where.
const MAX_LINE_CHARS: usize = 500;

/// Unterminated bytes tolerated before the line is cut short. Bounds `partial`
/// against a process that never sends a newline.
const MAX_PENDING_BYTES: usize = 8 * 1024;

/// The engine's recent stderr, shared between the pipe reader and the command
/// the window calls.
#[derive(Debug, Default)]
pub struct EngineLog {
    inner: Mutex<Buffer>,
}

#[derive(Debug, Default)]
struct Buffer {
    lines: VecDeque<String>,
    /// Bytes of a line whose newline has not arrived yet.
    partial: Vec<u8>,
}

impl EngineLog {
    /// Take in one read from the pipe, and hand back the lines it completed.
    ///
    /// Returning them is what lets the caller echo the engine's stderr as it
    /// arrives, from the one place that decides where a line ends — so the
    /// terminal copy and the retained copy cannot disagree about the split.
    pub fn feed(&self, chunk: &[u8]) -> Vec<String> {
        let mut buffer = self.lock();
        buffer.partial.extend_from_slice(chunk);
        let mut complete = Vec::new();
        while let Some(newline) = buffer.partial.iter().position(|b| *b == b'\n') {
            let taken: Vec<u8> = buffer.partial.drain(..=newline).collect();
            complete.push(finish_line(&taken[..newline]));
        }
        // No newline in sight and the buffer is already long: cut the line here
        // rather than wait for a read that may never come.
        if buffer.partial.len() > MAX_PENDING_BYTES {
            let taken = std::mem::take(&mut buffer.partial);
            complete.push(finish_line(&taken));
        }
        for line in complete.iter() {
            buffer.push(line.clone());
        }
        complete
    }

    /// The last `max` lines, oldest first — the order they were said in.
    ///
    /// Clamped rather than trusted: this is reached from a window that can send
    /// any number it likes, and an ask for a million lines must not be a
    /// million lines of allocation.
    pub fn tail(&self, max: usize) -> Vec<String> {
        let buffer = self.lock();
        let take = max.min(CAPACITY);
        buffer
            .lines
            .iter()
            .skip(buffer.lines.len().saturating_sub(take))
            .cloned()
            .collect()
    }

    /// Forget everything, including a half-read line.
    ///
    /// Called when a launch attempt starts: the previous run's last words are
    /// not this run's diagnosis, and a banner that quoted them would be
    /// confidently wrong.
    pub fn clear(&self) {
        let mut buffer = self.lock();
        buffer.lines.clear();
        buffer.partial.clear();
    }

    /// A reader that panicked must not take the window's read down with it.
    fn lock(&self) -> std::sync::MutexGuard<'_, Buffer> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }
}

impl Buffer {
    fn push(&mut self, line: String) {
        if self.lines.len() == CAPACITY {
            self.lines.pop_front();
        }
        self.lines.push_back(line);
    }
}

/// Decode one line's bytes, without its newline, and bound its length.
fn finish_line(bytes: &[u8]) -> String {
    // A `\r` is a progress bar redrawing, not text anyone wants to read.
    let bytes = match bytes.strip_suffix(b"\r") {
        Some(trimmed) => trimmed,
        None => bytes,
    };
    let text = String::from_utf8_lossy(bytes);
    if text.chars().count() <= MAX_LINE_CHARS {
        return text.into_owned();
    }
    let mut short: String = text.chars().take(MAX_LINE_CHARS).collect();
    short.push('…');
    short
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fed(log: &EngineLog, chunks: &[&[u8]]) -> Vec<String> {
        let mut lines = Vec::new();
        for chunk in chunks {
            lines.extend(log.feed(chunk));
        }
        lines
    }

    #[test]
    fn the_shutdown_warning_is_kept_verbatim() {
        let log = EngineLog::default();
        let said = fed(
            &log,
            &[b"[engine] shutdown unfinished after 6s \xe2\x80\x94 exiting anyway\n"],
        );
        assert_eq!(1, said.len());
        assert_eq!(
            vec!["[engine] shutdown unfinished after 6s — exiting anyway".to_string()],
            log.tail(10)
        );
    }

    #[test]
    fn a_line_split_across_reads_is_still_one_line() {
        // A pipe hands over whatever happened to be in the buffer, so the read
        // boundary has nothing to do with where the engine's lines are.
        let log = EngineLog::default();
        let said = fed(&log, &[b"[engine] shut", b"down un", b"finished\n"]);
        assert_eq!(vec!["[engine] shutdown unfinished".to_string()], said);
        assert_eq!(1, log.tail(10).len());
    }

    #[test]
    fn a_multibyte_character_split_across_reads_is_not_mangled() {
        // Decoding each read on its own would give "cafÃ©" here: the two bytes
        // of `é` are not a character until they are read together.
        let log = EngineLog::default();
        let said = fed(&log, &[b"caf\xc3", b"\xa9 ready\n"]);
        assert_eq!(vec!["café ready".to_string()], said);
    }

    #[test]
    fn the_oldest_line_is_dropped_when_the_buffer_is_full() {
        let log = EngineLog::default();
        for i in 0..CAPACITY + 5 {
            log.feed(format!("line {i}\n").as_bytes());
        }
        let tail = log.tail(CAPACITY);
        assert_eq!(CAPACITY, tail.len());
        assert_eq!(
            "line 5", tail[0],
            "the five oldest lines are the ones dropped"
        );
        assert_eq!(format!("line {}", CAPACITY + 4), tail[CAPACITY - 1]);
    }

    #[test]
    fn a_line_that_never_ends_is_cut_rather_than_grown() {
        let log = EngineLog::default();
        let flood = vec![b'x'; MAX_PENDING_BYTES * 3];
        let said = fed(&log, &[&flood]);
        assert_eq!(1, said.len(), "the flood is one line, not three");
        let kept = log.tail(10);
        assert_eq!(1, kept.len());
        assert!(
            kept[0].chars().count() <= MAX_LINE_CHARS + 1,
            "a truncated line ends in an ellipsis and stays short: {} chars",
            kept[0].chars().count()
        );
        assert!(kept[0].ends_with('…'));
    }

    #[test]
    fn a_very_long_line_is_kept_as_a_prefix_with_an_ellipsis() {
        let log = EngineLog::default();
        let long = "y".repeat(MAX_LINE_CHARS * 2);
        log.feed(format!("{long}\nnext\n").as_bytes());
        let tail = log.tail(10);
        assert_eq!(2, tail.len(), "the next line is still a separate line");
        assert!(tail[0].ends_with('…'));
        assert_eq!(MAX_LINE_CHARS + 1, tail[0].chars().count());
        assert_eq!("next", tail[1]);
    }

    #[test]
    fn a_carriage_return_is_not_part_of_the_line() {
        let log = EngineLog::default();
        log.feed(b"Uvicorn running on http://127.0.0.1:7430\r\n");
        assert_eq!(
            vec!["Uvicorn running on http://127.0.0.1:7430".to_string()],
            log.tail(10)
        );
    }

    #[test]
    fn tail_answers_a_smaller_ask_and_refuses_a_huge_one() {
        let log = EngineLog::default();
        for i in 0..10 {
            log.feed(format!("line {i}\n").as_bytes());
        }
        assert_eq!(3, log.tail(3).len());
        assert_eq!("line 7", log.tail(3)[0], "the last three, oldest first");
        assert!(log.tail(0).is_empty());
        assert_eq!(
            CAPACITY.min(10),
            log.tail(usize::MAX).len(),
            "an absurd ask is clamped to what is actually held"
        );
    }

    #[test]
    fn a_panicking_reader_does_not_take_the_reads_with_it() {
        let log = std::sync::Arc::new(EngineLog::default());
        log.feed(b"before\n");
        let writer = log.clone();
        let _ = std::thread::spawn(move || {
            // A panic *while the lock is held* is what poisons a mutex. Panicking
            // after `feed` returned would leave the lock clean and prove nothing.
            let mut guard = writer.lock();
            guard.push("during".to_string());
            panic!("reader died mid-write");
        })
        .join();
        assert!(log.inner.is_poisoned(), "the test must actually poison it");
        // Poisoned, not broken: the window still gets its lines.
        assert_eq!(vec!["during".to_string()], log.tail(1));
    }

    #[test]
    fn clearing_drops_the_previous_run_including_its_half_line() {
        let log = EngineLog::default();
        log.feed("[engine] parent process 42 is gone — exiting\npartial".as_bytes());
        log.clear();
        assert!(log.tail(CAPACITY).is_empty());
        // And the bytes that were pending are not prepended to the next run.
        let said = fed(&log, &[b"new run\n"]);
        assert_eq!(vec!["new run".to_string()], said);
    }
}
