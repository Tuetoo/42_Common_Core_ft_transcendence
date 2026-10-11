import { proxyDocumentFile } from "../../file-proxy";

export async function GET(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return proxyDocumentFile(id, "preview");
}
