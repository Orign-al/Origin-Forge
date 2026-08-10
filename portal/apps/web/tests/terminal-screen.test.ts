import { describe, expect, it } from "vitest";

import { TerminalScreen } from "../lib/terminal-screen";

describe("TerminalScreen", () => {
  it("renders shell output and applies carriage-return updates", () => {
    const screen = new TerminalScreen(20, 4);
    screen.feed("origin-pilot$ pwd\r\n/workspace\r\n");
    expect(screen.render()).toContain("origin-pilot$ pwd");
    expect(screen.render()).toContain("/workspace");

    screen.feed("progress 10%\rprogress 100%\r\n");
    expect(screen.render()).toContain("progress 100%");
    expect(screen.render()).not.toContain("progress 10%");
  });

  it("handles common cursor and erase controls without exposing OSC content", () => {
    const screen = new TerminalScreen(16, 4);
    screen.feed("secret\u001b[2K\rvisible");
    screen.feed("\u001b]0;<script>alert(1)</script>\u0007");
    screen.feed("\u001bP1;2|hidden-device-control\u001b\\");
    expect(screen.render()).toContain("visible");
    expect(screen.render()).not.toContain("secret");
    expect(screen.render()).not.toContain("script");
    expect(screen.render()).not.toContain("hidden-device-control");
  });

  it("restores the main screen after an alternate-screen program exits", () => {
    const screen = new TerminalScreen(20, 4);
    screen.feed("shell prompt");
    screen.feed("\u001b[?1049hfull screen app\u001b[?1049l");
    expect(screen.render()).toContain("shell prompt");
    expect(screen.render()).not.toContain("full screen app");
  });

  it("bounds hostile control counts to the configured screen", () => {
    const screen = new TerminalScreen(40, 10);
    screen.feed("safe\u001b[999999L\u001b[999999@");
    expect(screen.render().length).toBeLessThanOrEqual(410);
  });
});
