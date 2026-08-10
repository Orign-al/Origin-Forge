type ParserState =
  | "NORMAL"
  | "ESCAPE"
  | "CSI"
  | "OSC"
  | "OSC_ESCAPE"
  | "CONTROL_STRING"
  | "CONTROL_STRING_ESCAPE";

type SavedScreen = {
  screen: string[][];
  scrollback: string[];
  cursorX: number;
  cursorY: number;
};

const blankLine = (cols: number) => Array.from({ length: cols }, () => " ");

function parameterList(value: string) {
  const normalized = value.replace(/^[?>!]/, "");
  if (!normalized) return [0];
  return normalized.split(";").map((part) => {
    const parsed = Number.parseInt(part || "0", 10);
    return Number.isFinite(parsed) ? parsed : 0;
  });
}

export class TerminalScreen {
  private screen: string[][];
  private scrollback: string[] = [];
  private cursorX = 0;
  private cursorY = 0;
  private savedX = 0;
  private savedY = 0;
  private parserState: ParserState = "NORMAL";
  private control = "";
  private mainScreen: SavedScreen | null = null;

  constructor(
    private cols: number,
    private rows: number,
    private readonly scrollbackLimit = 5000,
  ) {
    this.screen = Array.from({ length: rows }, () => blankLine(cols));
  }

  clear() {
    this.screen = Array.from({ length: this.rows }, () => blankLine(this.cols));
    this.scrollback = [];
    this.cursorX = 0;
    this.cursorY = 0;
    this.parserState = "NORMAL";
    this.control = "";
    this.mainScreen = null;
  }

  resize(cols: number, rows: number) {
    if (cols === this.cols && rows === this.rows) return;
    const next = Array.from({ length: rows }, (_, row) => {
      const existing = this.screen[row] ?? [];
      return Array.from(
        { length: cols },
        (_, column) => existing[column] ?? " ",
      );
    });
    this.cols = cols;
    this.rows = rows;
    this.screen = next;
    this.cursorX = Math.min(this.cursorX, cols - 1);
    this.cursorY = Math.min(this.cursorY, rows - 1);
  }

  feed(value: string) {
    for (const character of value) this.consume(character);
  }

  render(cursor = false) {
    const visible = this.screen.map((line) => [...line]);
    if (cursor && visible[this.cursorY]) {
      visible[this.cursorY][Math.min(this.cursorX, this.cols - 1)] = "█";
    }
    const lines = [
      ...this.scrollback,
      ...visible.map((line) => line.join("").replace(/\s+$/u, "")),
    ];
    while (lines.length > 1 && lines.at(-1) === "") lines.pop();
    return lines.join("\n");
  }

  private consume(character: string) {
    if (this.parserState === "CONTROL_STRING") {
      if (character === "\u001b") this.parserState = "CONTROL_STRING_ESCAPE";
      return;
    }
    if (this.parserState === "CONTROL_STRING_ESCAPE") {
      this.parserState = character === "\\" ? "NORMAL" : "CONTROL_STRING";
      return;
    }
    if (this.parserState === "OSC") {
      if (character === "\u0007") this.parserState = "NORMAL";
      else if (character === "\u001b") this.parserState = "OSC_ESCAPE";
      return;
    }
    if (this.parserState === "OSC_ESCAPE") {
      this.parserState = character === "\\" ? "NORMAL" : "OSC";
      return;
    }
    if (this.parserState === "CSI") {
      if (character >= "@" && character <= "~") {
        this.csi(this.control, character);
        this.control = "";
        this.parserState = "NORMAL";
      } else if (this.control.length < 128) {
        this.control += character;
      } else {
        this.control = "";
        this.parserState = "NORMAL";
      }
      return;
    }
    if (this.parserState === "ESCAPE") {
      this.escape(character);
      return;
    }
    if (character === "\u001b") {
      this.parserState = "ESCAPE";
    } else if (character === "\r") {
      this.cursorX = 0;
    } else if (
      character === "\n" ||
      character === "\u000b" ||
      character === "\u000c"
    ) {
      this.lineFeed();
    } else if (character === "\b") {
      this.cursorX = Math.max(0, this.cursorX - 1);
    } else if (character === "\t") {
      const spaces = 8 - (this.cursorX % 8);
      for (let index = 0; index < spaces; index += 1) this.put(" ");
    } else if (character >= " " && character !== "\u007f") {
      this.put(character);
    }
  }

  private escape(character: string) {
    if (character === "[") {
      this.control = "";
      this.parserState = "CSI";
      return;
    }
    if (character === "]") {
      this.parserState = "OSC";
      return;
    }
    if (["P", "X", "^", "_"].includes(character)) {
      this.parserState = "CONTROL_STRING";
      return;
    }
    if (character === "7") {
      this.savedX = this.cursorX;
      this.savedY = this.cursorY;
    } else if (character === "8") {
      this.cursorX = this.savedX;
      this.cursorY = this.savedY;
    } else if (character === "D") {
      this.lineFeed();
    } else if (character === "E") {
      this.cursorX = 0;
      this.lineFeed();
    } else if (character === "M") {
      this.reverseLineFeed();
    } else if (character === "c") {
      this.clear();
    }
    this.parserState = "NORMAL";
  }

  private csi(raw: string, final: string) {
    const values = parameterList(raw);
    const first = Math.max(
      1,
      Math.min(values[0] || 1, Math.max(this.cols, this.rows)),
    );
    if (final === "A") this.cursorY = Math.max(0, this.cursorY - first);
    else if (final === "B")
      this.cursorY = Math.min(this.rows - 1, this.cursorY + first);
    else if (final === "C")
      this.cursorX = Math.min(this.cols - 1, this.cursorX + first);
    else if (final === "D") this.cursorX = Math.max(0, this.cursorX - first);
    else if (final === "E") {
      this.cursorY = Math.min(this.rows - 1, this.cursorY + first);
      this.cursorX = 0;
    } else if (final === "F") {
      this.cursorY = Math.max(0, this.cursorY - first);
      this.cursorX = 0;
    } else if (final === "G") this.cursorX = Math.min(this.cols - 1, first - 1);
    else if (final === "d") this.cursorY = Math.min(this.rows - 1, first - 1);
    else if (final === "H" || final === "f") {
      this.cursorY = Math.min(this.rows - 1, (values[0] || 1) - 1);
      this.cursorX = Math.min(this.cols - 1, (values[1] || 1) - 1);
    } else if (final === "J") this.eraseDisplay(values[0] ?? 0);
    else if (final === "K") this.eraseLine(values[0] ?? 0);
    else if (final === "P") this.deleteCharacters(first);
    else if (final === "@") this.insertCharacters(first);
    else if (final === "X") this.eraseCharacters(first);
    else if (final === "L") this.insertLines(first);
    else if (final === "M") this.deleteLines(first);
    else if (final === "S") this.scroll(first);
    else if (final === "T") this.scrollDown(first);
    else if (final === "s") {
      this.savedX = this.cursorX;
      this.savedY = this.cursorY;
    } else if (final === "u") {
      this.cursorX = this.savedX;
      this.cursorY = this.savedY;
    } else if ((final === "h" || final === "l") && raw.startsWith("?1049")) {
      if (final === "h") this.enterAlternateScreen();
      else this.leaveAlternateScreen();
    }
  }

  private put(character: string) {
    if (this.cursorX >= this.cols) {
      this.cursorX = 0;
      this.lineFeed();
    }
    this.screen[this.cursorY][this.cursorX] = character;
    this.cursorX += 1;
  }

  private lineFeed() {
    if (this.cursorY < this.rows - 1) {
      this.cursorY += 1;
      return;
    }
    this.scroll(1);
  }

  private reverseLineFeed() {
    if (this.cursorY > 0) this.cursorY -= 1;
    else this.screen.unshift(blankLine(this.cols));
    this.screen = this.screen.slice(0, this.rows);
  }

  private scroll(count: number) {
    for (let index = 0; index < count; index += 1) {
      const removed = this.screen.shift();
      if (removed && this.mainScreen === null) {
        this.scrollback.push(removed.join("").replace(/\s+$/u, ""));
        if (this.scrollback.length > this.scrollbackLimit)
          this.scrollback.shift();
      }
      this.screen.push(blankLine(this.cols));
    }
  }

  private scrollDown(count: number) {
    for (let index = 0; index < count; index += 1) {
      this.screen.pop();
      this.screen.unshift(blankLine(this.cols));
    }
  }

  private eraseDisplay(mode: number) {
    if (mode === 2 || mode === 3) {
      this.screen = Array.from({ length: this.rows }, () =>
        blankLine(this.cols),
      );
      if (mode === 3) this.scrollback = [];
      return;
    }
    if (mode === 0) {
      this.screen[this.cursorY].fill(" ", this.cursorX);
      for (let row = this.cursorY + 1; row < this.rows; row += 1) {
        this.screen[row].fill(" ");
      }
    } else if (mode === 1) {
      for (let row = 0; row < this.cursorY; row += 1)
        this.screen[row].fill(" ");
      this.screen[this.cursorY].fill(" ", 0, this.cursorX + 1);
    }
  }

  private eraseLine(mode: number) {
    if (mode === 0) this.screen[this.cursorY].fill(" ", this.cursorX);
    else if (mode === 1)
      this.screen[this.cursorY].fill(" ", 0, this.cursorX + 1);
    else if (mode === 2) this.screen[this.cursorY].fill(" ");
  }

  private deleteCharacters(count: number) {
    const line = this.screen[this.cursorY];
    line.splice(this.cursorX, count);
    line.push(...blankLine(Math.min(count, this.cols - line.length)));
  }

  private insertCharacters(count: number) {
    const line = this.screen[this.cursorY];
    line.splice(this.cursorX, 0, ...blankLine(count));
    line.length = this.cols;
  }

  private eraseCharacters(count: number) {
    this.screen[this.cursorY].fill(
      " ",
      this.cursorX,
      Math.min(this.cols, this.cursorX + count),
    );
  }

  private insertLines(count: number) {
    this.screen.splice(
      this.cursorY,
      0,
      ...Array.from({ length: count }, () => blankLine(this.cols)),
    );
    this.screen.length = this.rows;
  }

  private deleteLines(count: number) {
    this.screen.splice(this.cursorY, count);
    while (this.screen.length < this.rows)
      this.screen.push(blankLine(this.cols));
  }

  private enterAlternateScreen() {
    if (this.mainScreen) return;
    this.mainScreen = {
      screen: this.screen.map((line) => [...line]),
      scrollback: [...this.scrollback],
      cursorX: this.cursorX,
      cursorY: this.cursorY,
    };
    this.screen = Array.from({ length: this.rows }, () => blankLine(this.cols));
    this.scrollback = [];
    this.cursorX = 0;
    this.cursorY = 0;
  }

  private leaveAlternateScreen() {
    if (!this.mainScreen) return;
    this.screen = this.mainScreen.screen;
    this.scrollback = this.mainScreen.scrollback;
    this.cursorX = this.mainScreen.cursorX;
    this.cursorY = this.mainScreen.cursorY;
    this.mainScreen = null;
  }
}
