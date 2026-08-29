"use client";

import {
  type FormEvent,
  type KeyboardEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import { Button, StatusBadge } from "@h100-portal/ui";
import {
  ApiError,
  closeSelfTerminal,
  resizeSelfTerminal,
  selfTerminalOutput,
  sendSelfTerminalInput,
  startSelfTerminal,
  type SelfTerminal,
} from "../lib/api";
import { TerminalScreen } from "../lib/terminal-screen";
import { randomUuid } from "../lib/random-uuid";
import { useI18n } from "../lib/i18n";
import { PageHeading, SectionCard } from "./PortalShell";

type TerminalState =
  "IDLE" | "CONNECTING" | "RUNNING" | "CLOSING" | "CLOSED" | "ERROR";

const clamp = (value: number, minimum: number, maximum: number) =>
  Math.max(minimum, Math.min(maximum, value));

function terminalSize(element: HTMLElement) {
  return {
    cols: clamp(Math.floor((element.clientWidth - 20) / 7.85), 20, 300),
    rows: clamp(Math.floor((element.clientHeight - 20) / 17), 5, 120),
  };
}

function decodeBase64(value: string) {
  const binary = window.atob(value);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

type Translate = ReturnType<typeof useI18n>["t"];

function safeMessage(error: unknown, t: Translate) {
  if (
    error instanceof ApiError &&
    ["RESOURCE_OWNERSHIP_REJECTED", "SELF_COMPUTE_CONTEXT_INVALID"].includes(
      error.code,
    )
  ) {
    return t("无法确认当前计算环境，请联系管理员。");
  }
  if (error instanceof ApiError) return t(error.message);
  return t("网页终端连接失败");
}

function inputChunks(value: string, maximumBytes = 4096) {
  const encoder = new TextEncoder();
  const chunks: string[] = [];
  let current = "";
  let size = 0;
  for (const character of value) {
    const nextSize = encoder.encode(character).length;
    if (current && size + nextSize > maximumBytes) {
      chunks.push(current);
      current = "";
      size = 0;
    }
    current += character;
    size += nextSize;
  }
  if (current) chunks.push(current);
  return chunks;
}

function specialKey(event: KeyboardEvent<HTMLTextAreaElement>) {
  const fixed: Record<string, string> = {
    Enter: "\r",
    Backspace: "\u007f",
    Tab: "\t",
    Escape: "\u001b",
    ArrowUp: "\u001b[A",
    ArrowDown: "\u001b[B",
    ArrowRight: "\u001b[C",
    ArrowLeft: "\u001b[D",
    Home: "\u001b[H",
    End: "\u001b[F",
    Delete: "\u001b[3~",
    PageUp: "\u001b[5~",
    PageDown: "\u001b[6~",
    F1: "\u001bOP",
    F2: "\u001bOQ",
    F3: "\u001bOR",
    F4: "\u001bOS",
    F5: "\u001b[15~",
    F6: "\u001b[17~",
    F7: "\u001b[18~",
    F8: "\u001b[19~",
    F9: "\u001b[20~",
    F10: "\u001b[21~",
    F11: "\u001b[23~",
    F12: "\u001b[24~",
  };
  if (fixed[event.key]) return fixed[event.key];
  if (event.ctrlKey && !event.shiftKey && event.key.toLowerCase() === "v")
    return null;
  if (
    event.ctrlKey &&
    !event.shiftKey &&
    !event.altKey &&
    event.key.length === 1
  ) {
    const upper = event.key.toUpperCase().charCodeAt(0);
    if (upper >= 64 && upper <= 95) return String.fromCharCode(upper - 64);
  }
  if (event.altKey && !event.ctrlKey && event.key.length === 1) {
    return `\u001b${event.key}`;
  }
  return null;
}

export function WebTerminal() {
  const { t } = useI18n();
  const tRef = useRef(t);
  const hostRef = useRef<HTMLDivElement | null>(null);
  const outputRef = useRef<HTMLPreElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const screenRef = useRef<TerminalScreen | null>(null);
  const decoderRef = useRef(new TextDecoder());
  const dimensionsRef = useRef({ cols: 120, rows: 32 });
  const sessionRef = useRef<SelfTerminal | null>(null);
  const outputAbortRef = useRef<AbortController | null>(null);
  const inputChainRef = useRef<Promise<void>>(Promise.resolve());
  const composingRef = useRef(false);
  const resizeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [display, setDisplay] = useState("");
  const [state, setState] = useState<TerminalState>("IDLE");
  const [session, setSession] = useState<SelfTerminal | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    tRef.current = t;
  }, [t]);

  const sendData = useCallback((data: string) => {
    const active = sessionRef.current;
    if (!active || !data) return;
    inputChainRef.current = inputChainRef.current
      .then(async () => {
        for (const chunk of inputChunks(data)) {
          if (sessionRef.current?.id !== active.id) return;
          await sendSelfTerminalInput(active.id, chunk);
        }
      })
      .catch((reason: unknown) => {
        if (sessionRef.current?.id === active.id) {
          sessionRef.current = null;
          setSession(null);
          outputAbortRef.current?.abort();
          void closeSelfTerminal(active.id).catch(() => undefined);
        }
        setError(safeMessage(reason, tRef.current));
        setState("ERROR");
      });
  }, []);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const dimensions = terminalSize(host);
    dimensionsRef.current = dimensions;
    const screen = new TerminalScreen(dimensions.cols, dimensions.rows);
    screen.feed(`${tRef.current("开发容器网页终端")}\r\n`);
    screen.feed(
      `${tRef.current("只连接自己的开发容器；宿主访问保持禁用，GPU 为 NONE。")}\r\n\r\n`,
    );
    screenRef.current = screen;
    setDisplay(screen.render());
    const observer = new ResizeObserver(() => {
      if (resizeTimerRef.current) clearTimeout(resizeTimerRef.current);
      resizeTimerRef.current = setTimeout(() => {
        const next = terminalSize(host);
        dimensionsRef.current = next;
        screen.resize(next.cols, next.rows);
        setDisplay(screen.render(Boolean(sessionRef.current)));
        const active = sessionRef.current;
        if (active) {
          void resizeSelfTerminal(active.id, next.cols, next.rows).catch(
            () => undefined,
          );
        }
      }, 120);
    });
    observer.observe(host);
    return () => {
      observer.disconnect();
      if (resizeTimerRef.current) clearTimeout(resizeTimerRef.current);
      outputAbortRef.current?.abort();
      const active = sessionRef.current;
      sessionRef.current = null;
      if (active) void closeSelfTerminal(active.id).catch(() => undefined);
      screenRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (outputRef.current)
      outputRef.current.scrollTop = outputRef.current.scrollHeight;
  }, [display]);

  async function pollOutput(active: SelfTerminal, abort: AbortController) {
    let cursor = 0;
    try {
      while (!abort.signal.aborted && sessionRef.current?.id === active.id) {
        const output = await selfTerminalOutput(
          active.id,
          cursor,
          abort.signal,
        );
        const screen = screenRef.current;
        if (output.data_b64 && screen) {
          screen.feed(
            decoderRef.current.decode(decodeBase64(output.data_b64), {
              stream: true,
            }),
          );
          setDisplay(screen.render(true));
        }
        cursor = output.cursor;
        if (!["RUNNING", "CLOSING"].includes(output.state)) {
          const trailing = decoderRef.current.decode();
          if (trailing && screen) screen.feed(trailing);
          sessionRef.current = null;
          setSession(null);
          outputAbortRef.current = null;
          setState(output.state === "ERROR" ? "ERROR" : "CLOSED");
          if (screen) {
            screen.feed(
              `\r\n[${tRef.current("终端已关闭：{reason}", { reason: output.reason ?? "PROCESS_EXITED" })}]\r\n`,
            );
            setDisplay(screen.render());
          }
          return;
        }
      }
    } catch (reason) {
      if (abort.signal.aborted) return;
      if (sessionRef.current?.id === active.id) {
        sessionRef.current = null;
        setSession(null);
      }
      outputAbortRef.current = null;
      void closeSelfTerminal(active.id).catch(() => undefined);
      setState("ERROR");
      setError(safeMessage(reason, tRef.current));
    }
  }

  async function start() {
    const screen = screenRef.current;
    if (!screen || state === "CONNECTING" || sessionRef.current) return;
    setError(null);
    setState("CONNECTING");
    screen.clear();
    screen.feed(
      `${t("正在验证 Portal 会话、租约、资源所有权和容器安全状态…")}\r\n`,
    );
    setDisplay(screen.render());
    try {
      const dimensions = dimensionsRef.current;
      const response = await startSelfTerminal({
        idempotency_key: randomUuid(),
        cols: dimensions.cols,
        rows: dimensions.rows,
      });
      const active = response.terminal;
      decoderRef.current = new TextDecoder();
      sessionRef.current = active;
      setSession(active);
      setState("RUNNING");
      setDisplay(screen.render(true));
      inputRef.current?.focus();
      const abort = new AbortController();
      outputAbortRef.current = abort;
      void pollOutput(active, abort);
    } catch (reason) {
      setState("ERROR");
      setError(safeMessage(reason, t));
      screen.feed(`\r\n${t("终端启动被拒绝。请检查租约和开发容器状态。")}\r\n`);
      setDisplay(screen.render());
    }
  }

  async function close() {
    const active = sessionRef.current;
    if (!active) return;
    setState("CLOSING");
    try {
      await closeSelfTerminal(active.id);
    } catch (reason) {
      setError(safeMessage(reason, t));
      setState(sessionRef.current?.id === active.id ? "RUNNING" : "ERROR");
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.nativeEvent.isComposing) return;
    const data = specialKey(event);
    if (!data) return;
    event.preventDefault();
    sendData(data);
  }

  function handleInput(event: FormEvent<HTMLTextAreaElement>) {
    if (composingRef.current) return;
    const value = event.currentTarget.value;
    event.currentTarget.value = "";
    sendData(value);
  }

  return (
    <>
      <PageHeading
        title="网页终端"
        description="浏览器内连接自己的长期开发容器"
        action={<StatusBadge value={state} />}
      />
      <div className="terminal-security-band" role="note">
        <div>
          <strong>{t("容器 Shell，不是宿主 Shell")}</strong>
          <div className="muted">
            {t(
              "固定以当前登录身份进入自己的开发容器；GPU、Docker、MUNGE 与宿主访问均不可用。",
            )}
          </div>
        </div>
        <div className="button-row">
          <Button
            tone="primary"
            disabled={
              state === "CONNECTING" ||
              state === "RUNNING" ||
              state === "CLOSING"
            }
            onClick={() => void start()}
          >
            {t("打开终端")}
          </Button>
          <Button disabled={state !== "RUNNING"} onClick={() => void close()}>
            {t("关闭终端")}
          </Button>
        </div>
      </div>
      <SectionCard
        title="开发容器 Shell"
        subtitle="空闲15分钟自动关闭；单次最长1小时；租约到期会立即终止"
        action={session ? <StatusBadge value="GPU NONE" /> : undefined}
      >
        <div
          ref={hostRef}
          className="web-terminal-frame"
          data-testid="web-terminal"
          onClick={() => inputRef.current?.focus()}
          role="application"
          aria-label={t("开发容器网页终端")}
        >
          <pre ref={outputRef} className="web-terminal-output" aria-live="off">
            {display}
          </pre>
          <textarea
            ref={inputRef}
            className="terminal-input-capture"
            aria-label={t("终端键盘输入")}
            disabled={state !== "RUNNING"}
            autoCapitalize="off"
            autoComplete="off"
            autoCorrect="off"
            spellCheck={false}
            onKeyDown={handleKeyDown}
            onInput={handleInput}
            onCompositionStart={() => {
              composingRef.current = true;
            }}
            onCompositionEnd={(event) => {
              composingRef.current = false;
              const value = event.currentTarget.value;
              event.currentTarget.value = "";
              sendData(value);
            }}
            onPaste={(event) => {
              event.preventDefault();
              sendData(event.clipboardData.getData("text/plain"));
            }}
          />
        </div>
        {error ? <div className="error-box terminal-error">{error}</div> : null}
        <div className="terminal-footnote">
          {t(
            "网页终端输入和输出不会写入 Portal 审计日志；审计仅记录会话打开、关闭、目标容器与安全结果。GPU任务仍须通过“作业”页面提交。",
          )}
        </div>
      </SectionCard>
    </>
  );
}
