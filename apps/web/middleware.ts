import { NextResponse, type NextRequest } from "next/server";

export function middleware(request: NextRequest) {
  const path = request.nextUrl.pathname;
  const publicPath =
    path.startsWith("/api/auth") ||
    path.startsWith("/api/admin") ||
    path.startsWith("/api/health") ||
    path.startsWith("/api/openapi.json") ||
    path.startsWith("/admin") ||
    path.startsWith("/docs") ||
    path.startsWith("/brand/") ||
    path === "/icon.svg" ||
    path.startsWith("/onboarding/") ||
    path.startsWith("/login") ||
    path.startsWith("/registro") ||
    (process.env.NODE_ENV !== "production" && path.startsWith("/onboarding"));
  if (publicPath) return NextResponse.next();

  if (process.env.NODE_ENV !== "production" && process.env.DEV_TENANT_ID) return NextResponse.next();
  if (process.env.NODE_ENV !== "production" && request.headers.get("x-tenant-id")) {
    return NextResponse.next();
  }

  const hasSession =
    request.cookies.has("next-auth.session-token") ||
    request.cookies.has("__Secure-next-auth.session-token");
  if (!hasSession && (path.startsWith("/api") || path !== "/")) {
    return NextResponse.redirect(new URL("/login", request.url));
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
