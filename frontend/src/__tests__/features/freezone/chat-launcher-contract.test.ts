import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const source = readFileSync("src/features/freezone/FreezoneShell.tsx", "utf8");

describe("freezone nie-dao launcher contract", () => {
  it("uses one video source for both the idle first frame and hover motion", () => {
    expect(source).toContain('src="/images/nie-dao-launcher.mp4"');
    expect(source).not.toContain("nie-dao-launcher-poster.png");
    expect(source).not.toContain("xia-dao");
    expect(source).not.toContain('src="/images/avatar-claw.png"');
    expect(source).not.toContain('src="/images/avatar-motion.mp4"');
    expect(source).toContain("const CHAT_LAUNCHER_VISUAL_WIDTH = 96");
    expect(source).toContain("const CHAT_LAUNCHER_VISUAL_HEIGHT = 35");
    expect(source).toContain("const CHAT_LAUNCHER_HIT_HEIGHT = 44");
    expect(source).toContain("brightness-[1.08]");
    expect(source).toContain("group-hover/nie-dao:brightness-[1.16]");
  });

  it("plays from frame zero on hover or focus and resets on exit", () => {
    expect(source).toContain("video.currentTime = 0");
    expect(source).toContain("void video.play().catch(() => undefined)");
    expect(source).toContain("video.pause()");
    expect(source).toContain("onMouseEnter={playMotion}");
    expect(source).toContain("onMouseLeave={stopMotion}");
    expect(source).toContain("onFocus={playMotion}");
    expect(source).toContain("onBlur={stopMotion}");
  });

  it("keeps the existing click and drag interactions", () => {
    expect(source).toContain("onPointerDown={handlePointerDown}");
    expect(source).toContain("onClick={handleClick}");
    expect(source).toContain("onClick();");
  });

  it("renders the 捏导 wordmark as real text over the video", () => {
    // 视频只留角色动画：烤进去的字形在 96×35 下会丢笔画（土/寸底横只有
    // 6px/13px），所以字样改由浏览器排版，字重和字体跟随 UI 字体栈。
    expect(source).not.toContain("xia-dao");
    expect(source).toContain("const CHAT_LAUNCHER_LABEL_LEFT = 40");
    expect(source).toContain("const CHAT_LAUNCHER_LABEL_TOP = 12");
    expect(source).toContain("const CHAT_LAUNCHER_LABEL_WIDTH = 43");
    expect(source).toContain("const CHAT_LAUNCHER_LABEL_FONT_SIZE = 20");
    expect(source).toContain('var(--font-family-ui-default)');
    expect(source).toMatch(/aria-hidden="true"[\s\S]{0,1200}?>\s*捏导\s*<\/span>/);
  });
});
