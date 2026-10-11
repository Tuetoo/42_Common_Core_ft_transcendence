"use client";

// The browser's print dialog has "Save as PDF" built in; the print styles
// in globals.css hide the sidebar and controls so only the report prints.
export function PrintButton() {
  return (
    <button type="button" className="btn btn-ghost" onClick={() => window.print()}>
      Save as PDF
    </button>
  );
}
