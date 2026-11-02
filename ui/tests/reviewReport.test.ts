/**
 * The deep-review card, mounted: what it says about which readers ran, and what it will not say.
 *
 * The property worth a test is the one the engine keeps and a UI could quietly lose: a reader that did not run is
 * shown as not run, and "no findings" is never drawn over a report in which a reader was missing without the
 * status line saying so. Everything is asserted on the text and roles a person reads.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { ReviewReportPayload } from "../src/components/ReviewReport.tsx";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;
const h = React.createElement;
const { ReviewReport, looksLikeReviewReport } = await import("../src/components/ReviewReport.tsx");
const { AuditReport } = await import("../src/components/AuditReport.tsx");

const lens = (name: string, status: string, findings = 0) => ({
  lens: name, status, findings, calls: status === "ran" ? 1 : 0, duration_ms: 4,
});

const FULL: ReviewReportPayload = {
  status: "3 of 3 lenses ran",
  detail: "",
  lenses: [lens("zoom-out", "ran", 1), lens("comprehension", "ran"), lens("readiness", "ran")],
  findings: [
    {
      severity: "major", category: "call-site", scope: "src/app.py:3",
      evidence: "greet() is still called", recommendation: "update the call site", lenses: ["zoom-out"],
    },
  ],
};

test("the status line, each reader and each finding are drawn", async () => {
  await withDom(async (dom) => {
    await dom.render(h(ReviewReport, { report: FULL }));
    assert.match(dom.text(), /3 of 3 lenses ran/);
    const readers = dom.container.querySelector('[aria-label="Reviewers"]');
    assert.ok(readers, "the reviewers list is missing");
    assert.match(readers.textContent ?? "", /zoom-out: 1 found/);
    assert.match(readers.textContent ?? "", /readiness: 0 found/);
    const findings = dom.container.querySelector('[aria-label="Findings"]');
    assert.ok(findings, "the findings list is missing");
    assert.match(findings.textContent ?? "", /major/);
    assert.match(findings.textContent ?? "", /src\/app\.py:3/);
    assert.match(findings.textContent ?? "", /update the call site/);
  });
});

test("a reader that did not run is shown as not run, with the status line saying so", async () => {
  const partial: ReviewReportPayload = {
    status: "2 of 3 lenses ran — comprehension not run",
    detail: "",
    lenses: [lens("zoom-out", "ran"), { ...lens("comprehension", "not_run"), detail: "reply was not JSON" }, lens("readiness", "ran")],
    findings: [],
  };
  await withDom(async (dom) => {
    await dom.render(h(ReviewReport, { report: partial }));
    assert.match(dom.text(), /2 of 3 lenses ran — comprehension not run/);
    assert.match(dom.text(), /comprehension: not run/);
    assert.doesNotMatch(dom.text(), /comprehension: 0 found/, "a reader that did not run was drawn as a clean one");
    assert.match(dom.text(), /No findings from the lenses that ran\./);
  });
});

test("a fall-back to the standard review is explained", async () => {
  const fellBack: ReviewReportPayload = {
    status: "1 of 3 lenses ran — zoom-out, comprehension not run",
    detail: "fell back to the standard single review: no model lens produced a usable reply",
    lenses: [lens("zoom-out", "not_run"), lens("comprehension", "not_run"), lens("readiness", "ran")],
    findings: [],
  };
  await withDom(async (dom) => {
    await dom.render(h(ReviewReport, { report: fellBack }));
    assert.match(dom.text(), /fell back to the standard single review/);
  });
});

test("an event of the wrong shape is not drawn", () => {
  assert.equal(looksLikeReviewReport(FULL), true);
  for (const bad of [null, undefined, "x", {}, { status: "s" }, { status: "s", lenses: [], findings: "no" }]) {
    assert.equal(looksLikeReviewReport(bad), false);
  }
});

test("the audit report shows a deep-review section only when there is a report", async () => {
  await withDom(async (dom) => {
    await dom.render(h(AuditReport, { doc: { goal_id: "g", final_status: "COMPLETED", step_outcomes: [] } }));
    assert.doesNotMatch(dom.text(), /Deep review/);
    await dom.render(
      h(AuditReport, {
        doc: { goal_id: "g", final_status: "COMPLETED", step_outcomes: [], review_reports: [{ ...FULL, step: "Rename (s1)" }] },
      }),
    );
    assert.match(dom.text(), /Deep review/);
    assert.match(dom.text(), /Rename/);
    assert.match(dom.text(), /update the call site/);
  });
});
