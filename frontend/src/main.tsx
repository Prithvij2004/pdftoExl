import { QueryClient, QueryClientProvider, useMutation } from "@tanstack/react-query";
import { ChangeEvent, FormEvent, StrictMode, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const queryClient = new QueryClient();

type ExtractResult = {
  blob: Blob;
  filename: string;
};

async function extractPdf(file: File): Promise<ExtractResult> {
  const formData = new FormData();
  formData.append("file", file);

  const response = await fetch("/api/extract", {
    method: "POST",
    body: formData
  });

  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(error?.detail ?? "Could not extract this PDF.");
  }

  const disposition = response.headers.get("content-disposition") ?? "";
  const match = disposition.match(/filename="?([^"]+)"?/);

  return {
    blob: await response.blob(),
    filename: match?.[1] ?? file.name.replace(/\.pdf$/i, ".xlsx")
  };
}

function downloadBlob({ blob, filename }: ExtractResult) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function App() {
  const [file, setFile] = useState<File | null>(null);

  const extractMutation = useMutation({
    mutationFn: extractPdf,
    onSuccess: downloadBlob
  });

  const fileSize = useMemo(() => {
    if (!file) return "";
    return `${(file.size / 1024 / 1024).toFixed(2)} MB`;
  }, [file]);

  function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    const selected = event.target.files?.[0] ?? null;
    setFile(selected);
    extractMutation.reset();
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (file) {
      extractMutation.mutate(file);
    }
  }

  return (
    <main className="shell">
      <section className="card">
        <p className="eyebrow">PDF to Excel</p>
        <h1>Extract form fields into a clean spreadsheet.</h1>
        <p className="lede">
          Upload a PDF form and download the generated Excel workbook when extraction finishes.
        </p>

        <form className="upload-form" onSubmit={handleSubmit}>
          <label className="dropzone">
            <input accept="application/pdf" type="file" onChange={handleFileChange} />
            <span className="dropzone-title">{file ? file.name : "Choose a PDF file"}</span>
            <span className="dropzone-subtitle">
              {file ? fileSize : "Only PDF uploads are supported"}
            </span>
          </label>

          <button type="submit" disabled={!file || extractMutation.isPending}>
            {extractMutation.isPending ? "Extracting..." : "Generate Excel"}
          </button>
        </form>

        {extractMutation.isError ? (
          <p className="status error">{extractMutation.error.message}</p>
        ) : null}
        {extractMutation.isSuccess ? (
          <p className="status success">Excel file generated and downloaded.</p>
        ) : null}
      </section>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>
);
