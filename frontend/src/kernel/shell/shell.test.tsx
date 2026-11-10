import { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../../App";
import { mockFetch } from "../../test/mockApi";
import { applyTheme, getTheme, setTheme, toggleTheme } from "../theme";

const session = {
  user: {
    id: 2,
    login: "admin",
    name: "Admin",
    email: null,
    lang: "en_US",
    tz: "UTC",
    share: false,
    totp_enabled: false,
    is_admin: true,
  },
  company: { id: 1, name: "Main" },
  allowed_companies: [
    { id: 1, name: "Main" },
    { id: 2, name: "Europe" },
  ],
  selected_companies: [1],
  groups: [],
  auth: "session",
  csrf_token: "csrf",
  lang: "en_US",
  modules: ["core", "web"],
};
const menus = [
  {
    id: 1,
    xmlid: "web.menu_settings_root",
    name: "Settings",
    sequence: 1,
    icon: null,
    action: null,
    action_id: null,
    children: [
      {
        id: 2,
        xmlid: null,
        name: "Users",
        sequence: 1,
        icon: null,
        action: "web.action_users",
        action_id: 5,
        children: [],
      },
    ],
  },
];

function renderApp() {
  window.history.pushState({}, "", "/");
  return render(<App queryClient={new QueryClient({ defaultOptions: { queries: { retry: false } } })} />);
}

describe("login and shell", () => {
  beforeEach(() => window.localStorage.clear());

  it("logs in with password then TOTP and shows the home page", async () => {
    let loggedIn = false;
    const { calls } = mockFetch({
      "GET /api/v1/auth/session": () =>
        loggedIn
          ? session
          : new Response(JSON.stringify({ error: "AccessDenied", message: "Authentication required" }), {
              status: 401,
            }),
      "GET /api/v1/auth/providers": () => [{ code: "okta", name: "Okta" }],
      "GET /api/v1/auth/saml/providers": () => [{ id: 3, name: "Contoso SAML" }],
      "POST /api/v1/auth/login": () => ({ totp_required: true, pre_auth_token: "pre" }),
      "POST /api/v1/auth/totp": () => {
        loggedIn = true;
        return session;
      },
      "GET /api/v1/web/menus": () => menus,
    });
    renderApp();
    expect(await screen.findByRole("heading", { name: "Sign in to Tessera" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Okta" })).toHaveAttribute("href", "/api/v1/auth/oidc/okta/start");
    expect(await screen.findByRole("link", { name: "Contoso SAML" })).toHaveAttribute(
      "href",
      "/api/v1/auth/saml/3/login",
    );
    await userEvent.type(screen.getByLabelText("Login"), "admin");
    await userEvent.type(screen.getByLabelText("Password"), "admin1234");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("heading", { name: "Two-factor authentication" })).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/Authentication code/), "123456");
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByRole("heading", { name: "Welcome, Admin" })).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/v1/auth/totp")!.body).toEqual({ pre_auth_token: "pre", code: "123456" });
    expect((await screen.findAllByRole("button", { name: "Settings" })).length).toBe(2); // top bar + home tile
  });

  it("shows login errors", async () => {
    mockFetch({
      "GET /api/v1/auth/session": () =>
        new Response(JSON.stringify({ error: "AccessDenied", message: "x" }), { status: 401 }),
      "GET /api/v1/auth/providers": () => [],
      "POST /api/v1/auth/login": () =>
        new Response(JSON.stringify({ error: "AccessDenied", message: "Invalid login or password" }), { status: 401 }),
    });
    renderApp();
    await userEvent.type(await screen.findByLabelText("Login"), "admin");
    await userEvent.type(screen.getByLabelText("Password"), "nope");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid login or password");
  });

  it("offers SAML providers on their own and hides the list when SAML is not installed", async () => {
    mockFetch({
      "GET /api/v1/auth/session": () =>
        new Response(JSON.stringify({ error: "AccessDenied", message: "x" }), { status: 401 }),
      "GET /api/v1/auth/providers": () => [],
      "GET /api/v1/auth/saml/providers": () => [{ id: 7, name: "Corporate SSO" }],
    });
    const { unmount } = renderApp();
    expect(await screen.findByRole("link", { name: "Corporate SSO" })).toHaveAttribute(
      "href",
      "/api/v1/auth/saml/7/login",
    );
    expect(screen.getByText("or continue with")).toBeInTheDocument();
    unmount();
    mockFetch({
      "GET /api/v1/auth/session": () =>
        new Response(JSON.stringify({ error: "AccessDenied", message: "x" }), { status: 401 }),
      "GET /api/v1/auth/providers": () => [],
    });
    renderApp();
    expect(await screen.findByRole("heading", { name: "Sign in to Tessera" })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("or continue with")).not.toBeInTheDocument());
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("switches companies, opens the palette and signs out", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/auth/session": () => session,
      "GET /api/v1/web/menus": () => menus,
      "POST /api/v1/auth/logout": () => ({ ok: true }),
      "GET /api/v1/web/actions/web.action_users": () =>
        new Response(JSON.stringify({ error: "MissingError", message: "gone" }), { status: 404 }),
    });
    renderApp();
    expect(await screen.findByRole("heading", { name: "Welcome, Admin" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Main" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Include Europe" }));
    await waitFor(() =>
      expect(calls.filter((c) => c.path === "/api/v1/auth/session").at(-1)!.headers["X-Tessera-Companies"]).toBe("1,2"),
    );
    expect(JSON.parse(window.localStorage.getItem("tessera.companies")!)).toEqual({ ids: [1, 2], current: 1 });
    await userEvent.keyboard("{Control>}k{/Control}");
    expect(await screen.findByRole("dialog", { name: "Command palette" })).toBeInTheDocument();
    await userEvent.keyboard("users{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("gone");
    await userEvent.click(screen.getByRole("button", { name: "Admin" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Sign out" }));
    expect(await screen.findByRole("heading", { name: "Sign in to Tessera" })).toBeInTheDocument();
  });

  it("toggles the theme", () => {
    setTheme("dark");
    expect(getTheme()).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(toggleTheme()).toBe("light");
    setTheme("system");
    applyTheme();
    expect(document.documentElement.dataset.theme).toBeUndefined();
  });
});
