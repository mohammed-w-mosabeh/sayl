import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Details } from "@/components/details";
import { DownloadList } from "@/components/download-list";
import { ApiProvider } from "@/components/providers";
import { makeApi } from "@/lib/api/client";
import { I18nProvider, type Language } from "@/lib/i18n";
import arabic from "@/lib/i18n/ar.json";
import { download } from "@/test/fixtures";

const connection = { baseUrl: "http://127.0.0.1:1", token: "t" };

function renderIn(language: Language, ui: React.ReactNode) {
  const api = makeApi(connection, () => Promise.reject(new Error("offline")));
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <I18nProvider language={language}>
        <ApiProvider api={api} connection={connection}>
          {ui}
        </ApiProvider>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

describe("Arabic layout", () => {
  it("sets the document to right to left and translates the list", () => {
    renderIn("ar", <DownloadList rows={[download()]} empty="none" />);
    expect(document.documentElement.dir).toBe("rtl");
    expect(document.documentElement.lang).toBe("ar");
    expect(screen.getByText(arabic["list.column.name"])).toBeTruthy();
    expect(screen.getByText(arabic["status.active"])).toBeTruthy();
    expect(screen.getByLabelText(arabic["action.pause"])).toBeTruthy();
  });

  it("isolates file names and links from the Arabic text direction", () => {
    renderIn("ar", <Details row={download({ engine: "hybrid", connections: 4 })} />);
    expect(screen.getByText("ubuntu.iso", { selector: "h2" }).getAttribute("dir")).toBe("auto");
    expect(screen.getByText("https://example.com/files/ubuntu.iso").getAttribute("dir")).toBe("ltr");
    expect(screen.getByText(arabic["details.mode.accelerated"].replace("{count}", "4"))).toBeTruthy();
  });

  it("switches back to left to right in English", () => {
    renderIn("en", <DownloadList rows={[]} empty="none" />);
    expect(document.documentElement.dir).toBe("ltr");
    expect(screen.getByText("No downloads yet")).toBeTruthy();
  });
});
