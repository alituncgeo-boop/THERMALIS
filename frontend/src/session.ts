import { api } from "./api";

/**
 * `thermalis start` tarayıcıyı tek kullanımlık bir kodla (`#auth=<kod>`) açar. Kod sunucuya bir kez
 * gönderilir, karşılığında yalnız HttpOnly bir oturum çerezi alınır ve kod adres çubuğundan hemen silinir.
 * Bu bir kimlik doğrulaması değildir: yalnız isteğin bu bilgisayarda başlatılan yerel oturumdan geldiğini gösterir.
 */
export function takeAuthCode(location: Pick<Location, "hash" | "pathname" | "search">): string | null {
  const match = /^#auth=([A-Za-z0-9_-]{16,128})$/.exec(location.hash);
  return match ? match[1] : null;
}

export async function claimSessionFromLocation(): Promise<void> {
  if (typeof window === "undefined") return;
  const code = takeAuthCode(window.location);
  if (!code) return;
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  try {
    await api.claimSession(code);
  } catch {
    /* geçersiz/süresi dolmuş kod: doğrulama kapalı kalır, arayüz salt okunur çalışır */
  }
}
