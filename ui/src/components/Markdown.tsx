import React, { useEffect, useMemo, useRef, useState } from "react";
import { Check, Copy } from "lucide-react";
import { parseInline, parseMarkdown, type Block, type Inline, type ListItem } from "../markdown";
import { linkAction } from "../markdownLinks";
import { copySchemeText } from "../scheme";
import { useClipboardRecorder } from "../clipboardContext";

/**
 * Markdown, drawn as React elements and nothing else.
 *
 * `markdown.ts` produces a tree and this turns it into elements, so every string a model wrote goes
 * through React's own escaping and none through `innerHTML`. There is no `dangerouslySetInnerHTML`
 * here, no `<img>` (an image is shown as its alt text, and nothing is fetched), and no `<a href>`: a
 * link is a button that asks the app to open the address in its own browser tab (`markdownLinks.ts`
 * says why, and what is allowed). `ui/tests/markdownRender.test.tsx`'s hostile cases mount this with
 * `<script>`, `javascript:` links and event-handler attributes and check none becomes live.
 *
 * It is drawn inside a surface that already has a colour (the answer bubble, a card), so it sets
 * no background of its own except for code, where the box is the point.
 */

export interface MarkdownProps {
  text: string;
  /**
   * Called with an address the link policy accepted, when its link is clicked. Without it a link is
   * its words only: a place that cannot open a tab has no business drawing something clickable.
   */
  onOpenLink?: (url: string) => void;
  /** Inline marks only (bold, code, links), in a `<span>`: for a sentence, not a document. */
  inline?: boolean;
  className?: string;
}

interface LinkProps {
  onOpenLink?: (url: string) => void;
}

const INLINE_CODE =
  "rounded bg-codify-raised px-1 py-px font-mono text-[0.9em] text-codify-primary";

function InlineNodes({ nodes, onOpenLink }: { nodes: readonly Inline[] } & LinkProps): React.ReactElement {
  return (
    <>
      {nodes.map((node, i) => {
        switch (node.kind) {
          case "text":
            return <React.Fragment key={i}>{node.text}</React.Fragment>;
          case "br":
            return <br key={i} />;
          case "code":
            return (
              <code key={i} className={INLINE_CODE}>
                {node.text}
              </code>
            );
          case "strong":
            return (
              <strong key={i} className="font-semibold text-codify-primary">
                <InlineNodes nodes={node.children} onOpenLink={onOpenLink} />
              </strong>
            );
          case "em":
            return (
              <em key={i}>
                <InlineNodes nodes={node.children} onOpenLink={onOpenLink} />
              </em>
            );
          case "del":
            return (
              <del key={i} className="text-codify-muted">
                <InlineNodes nodes={node.children} onOpenLink={onOpenLink} />
              </del>
            );
          case "image":
            // Never an <img>: the alt text, so the answer still says what the picture was for.
            return (
              <span key={i} className="italic text-codify-muted" title={node.src}>
                [image: {node.alt || "no description"}]
              </span>
            );
          case "link": {
            const action = linkAction(node.href);
            if (action.kind === "open" && onOpenLink) {
              return (
                <button
                  key={i}
                  type="button"
                  onClick={() => onOpenLink(action.url)}
                  title={`Open ${action.url} in a browser tab`}
                  className="inline cursor-pointer break-words text-left text-codify-info underline underline-offset-2 hover:brightness-125"
                >
                  <InlineNodes nodes={node.children} onOpenLink={undefined} />
                </button>
              );
            }
            // Not openable, or nowhere to open it: the words, with the address and the reason in the
            // tooltip so a refused link can still be read and copied.
            return (
              <span
                key={i}
                title={action.kind === "open" ? node.href : `${node.href} (${action.why})`}
                className="underline decoration-dotted underline-offset-2"
              >
                <InlineNodes nodes={node.children} onOpenLink={undefined} />
              </span>
            );
          }
        }
      })}
    </>
  );
}

/** A fenced block: language label, copy button, and the code in a box that scrolls sideways. */
function CodeBlock({ lang, text }: { lang: string; text: string }): React.ReactElement {
  const [copied, setCopied] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const record = useClipboardRecorder();

  const copy = async (): Promise<void> => {
    const clipboard =
      typeof navigator !== "undefined" && navigator.clipboard
        ? navigator.clipboard.writeText.bind(navigator.clipboard)
        : undefined;
    const outcome = await copySchemeText(text, {
      writeText: clipboard,
      document: typeof document !== "undefined" ? document : undefined,
    });
    // `writeText` fires no `copy` event, so the clipboard history hears of this one from here, and only once it
    // worked: a copy that failed is not on the clipboard either.
    if (outcome !== "failed") record(text, "code");
    setCopied(outcome === "failed" ? "failed" : "copied");
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied("idle"), 1800);
  };

  return (
    <div className="overflow-hidden rounded-lg border border-codify-border">
      <div className="flex items-center justify-between gap-2 border-b border-codify-border bg-codify-raised px-2.5 py-1 font-mono text-2xs text-codify-muted">
        <span className="truncate">{lang || "code"}</span>
        <button
          type="button"
          onClick={() => void copy()}
          aria-label="Copy code"
          className="flex flex-shrink-0 items-center gap-1 rounded px-1 text-codify-muted transition-colors hover:text-codify-primary"
        >
          {copied === "copied" ? (
            <Check aria-hidden="true" className="h-3 w-3" />
          ) : (
            <Copy aria-hidden="true" className="h-3 w-3" />
          )}
          {copied === "copied" ? "Copied" : copied === "failed" ? "Copy failed" : "Copy"}
        </button>
      </div>
      <pre className="max-h-96 overflow-auto whitespace-pre bg-codify-bg p-2.5 font-mono text-xs leading-relaxed text-codify-secondary">
        <code>{text}</code>
      </pre>
    </div>
  );
}

const HEADING_CLASS: Record<1 | 2 | 3 | 4 | 5 | 6, string> = {
  1: "text-lg font-semibold text-codify-primary",
  2: "text-md font-semibold text-codify-primary",
  3: "text-base font-semibold text-codify-primary",
  4: "text-sm font-semibold text-codify-primary",
  5: "text-sm font-semibold text-codify-secondary",
  6: "text-sm font-semibold text-codify-secondary",
};

function ListNode({
  ordered,
  start,
  items,
  onOpenLink,
}: { ordered: boolean; start: number; items: ListItem[] } & LinkProps): React.ReactElement {
  const body = items.map((item, i) => (
    <li key={i} className="pl-0.5">
      <InlineNodes nodes={item.inlines} onOpenLink={onOpenLink} />
      {item.children.length > 0 && (
        <div className="mt-1 space-y-1">
          <Blocks blocks={item.children} onOpenLink={onOpenLink} />
        </div>
      )}
    </li>
  ));
  return ordered ? (
    <ol start={start} className="list-decimal space-y-1 pl-5">
      {body}
    </ol>
  ) : (
    <ul className="list-disc space-y-1 pl-5">{body}</ul>
  );
}

function Blocks({ blocks, onOpenLink }: { blocks: readonly Block[] } & LinkProps): React.ReactElement {
  return (
    <>
      {blocks.map((block, i) => {
        switch (block.kind) {
          case "heading": {
            const Tag = (`h${Math.min(6, block.level + 2)}` as "h3" | "h4" | "h5" | "h6");
            return (
              <Tag key={i} className={HEADING_CLASS[block.level]}>
                <InlineNodes nodes={block.inlines} onOpenLink={onOpenLink} />
              </Tag>
            );
          }
          case "paragraph":
            return (
              <p key={i}>
                <InlineNodes nodes={block.inlines} onOpenLink={onOpenLink} />
              </p>
            );
          case "list":
            return (
              <ListNode key={i} ordered={block.ordered} start={block.start} items={block.items} onOpenLink={onOpenLink} />
            );
          case "code":
            return <CodeBlock key={i} lang={block.lang} text={block.text} />;
          case "quote":
            return (
              <blockquote key={i} className="space-y-2 border-l-2 border-codify-border-strong pl-3 text-codify-secondary">
                <Blocks blocks={block.blocks} onOpenLink={onOpenLink} />
              </blockquote>
            );
          case "rule":
            return <hr key={i} className="border-codify-border" />;
        }
      })}
    </>
  );
}

export const Markdown: React.FC<MarkdownProps> = ({ text, onOpenLink, inline = false, className = "" }) => {
  // The engine's payloads are loosely typed (`payload.design_md` is `any`, a plan step can arrive
  // without a description), and drawing nothing is what `{undefined}` always did. A component that
  // throws on a missing field would take the whole transcript down with it.
  const source = typeof text === "string" ? text : "";
  const tree = useMemo(() => (inline ? null : parseMarkdown(source)), [source, inline]);
  const run = useMemo(() => (inline ? parseInline(source) : null), [source, inline]);
  if (inline) {
    return (
      <span className={"break-words " + className}>
        <InlineNodes nodes={run ?? []} onOpenLink={onOpenLink} />
      </span>
    );
  }
  return (
    <div className={"space-y-2 break-words " + className}>
      <Blocks blocks={tree ?? []} onOpenLink={onOpenLink} />
    </div>
  );
};
