(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.MindBridgeMarkdown = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function safeHref(value) {
    const href = String(value || "").trim();
    if (!/^(https?:|mailto:)/i.test(href)) return "";
    return escapeHtml(href);
  }

  function renderInline(value) {
    const codeSpans = [];
    let text = String(value).replace(/`([^`\n]+)`/g, (_, code) => {
      const index = codeSpans.push(`<code>${escapeHtml(code)}</code>`) - 1;
      return `\u0000CODE${index}\u0000`;
    });
    text = escapeHtml(text);
    text = text.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, href) => {
      const safe = safeHref(href);
      if (!safe) return label;
      return `<a href="${safe}" target="_blank" rel="noopener noreferrer">${label}</a>`;
    });
    text = text.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
    text = text.replace(/(^|[\s(])\*([^*\n]+)\*(?=$|[\s).,，。])/g, "$1<em>$2</em>");
    return text.replace(/\u0000CODE(\d+)\u0000/g, (_, index) => codeSpans[Number(index)]);
  }

  function listMatch(line) {
    const match = /^(\s*)([-+*]|\d+\.)\s+(.+)$/.exec(line);
    if (!match) return null;
    return {
      indent: match[1].replaceAll("\t", "    ").length,
      ordered: /\d+\./.test(match[2]),
      number: /^\d+\./.test(match[2]) ? Number.parseInt(match[2], 10) : null,
      content: match[3],
    };
  }

  function renderList(lines, start, baseIndent) {
    const first = listMatch(lines[start]);
    const tag = first.ordered ? "ol" : "ul";
    const startAttribute = first.ordered && first.number > 1 ? ` start="${first.number}"` : "";
    let html = `<${tag}${startAttribute}>`;
    let index = start;
    let itemOpen = false;

    while (index < lines.length) {
      const item = listMatch(lines[index]);
      if (!item || item.indent < baseIndent) break;
      if (item.indent > baseIndent) {
        if (!itemOpen) break;
        const nested = renderList(lines, index, item.indent);
        html += nested.html;
        index = nested.next;
        continue;
      }
      if (item.ordered !== first.ordered) break;
      if (itemOpen) html += "</li>";
      html += `<li>${renderInline(item.content)}`;
      itemOpen = true;
      index += 1;
    }
    if (itemOpen) html += "</li>";
    html += `</${tag}>`;
    return { html, next: index };
  }

  function startsBlock(line) {
    return (
      !line.trim() ||
      /^```/.test(line) ||
      /^#{1,6}\s+/.test(line) ||
      /^>\s?/.test(line) ||
      Boolean(listMatch(line))
    );
  }

  function render(source) {
    const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
    const blocks = [];
    let index = 0;

    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) {
        index += 1;
        continue;
      }

      const fence = /^```\s*([\w-]*)\s*$/.exec(line);
      if (fence) {
        const code = [];
        index += 1;
        while (index < lines.length && !/^```\s*$/.test(lines[index])) {
          code.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) index += 1;
        const language = fence[1] ? ` class="language-${escapeHtml(fence[1])}"` : "";
        blocks.push(`<pre><code${language}>${escapeHtml(code.join("\n"))}</code></pre>`);
        continue;
      }

      const heading = /^(#{1,6})\s+(.+)$/.exec(line);
      if (heading) {
        const level = heading[1].length;
        blocks.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
        index += 1;
        continue;
      }

      const item = listMatch(line);
      if (item) {
        const list = renderList(lines, index, item.indent);
        blocks.push(list.html);
        index = list.next;
        continue;
      }

      if (/^>\s?/.test(line)) {
        const quote = [];
        while (index < lines.length && /^>\s?/.test(lines[index])) {
          quote.push(lines[index].replace(/^>\s?/, ""));
          index += 1;
        }
        blocks.push(`<blockquote>${quote.map(renderInline).join("<br>")}</blockquote>`);
        continue;
      }

      const paragraph = [line.trim()];
      index += 1;
      while (index < lines.length && !startsBlock(lines[index])) {
        paragraph.push(lines[index].trim());
        index += 1;
      }
      blocks.push(`<p>${paragraph.map(renderInline).join("<br>")}</p>`);
    }
    return blocks.join("");
  }

  return { escapeHtml, render };
});
