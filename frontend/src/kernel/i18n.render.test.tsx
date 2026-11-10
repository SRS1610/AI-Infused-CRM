import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../App";
import { mockFetch } from "../test/mockApi";
import type { ViewsResponse } from "./api/types";
import { setLanguage } from "./i18n";
import { ListView } from "./views/ListView";

const session = {
  user: {
    id: 2,
    login: "marie",
    name: "Marie",
    email: null,
    lang: "fr_FR",
    tz: "Europe/Paris",
    share: false,
    totp_enabled: false,
    is_admin: false,
  },
  company: { id: 1, name: "Main" },
  allowed_companies: [{ id: 1, name: "Main" }],
  selected_companies: [1],
  groups: [],
  auth: "session",
  csrf_token: "csrf",
  lang: "fr_FR",
  modules: ["core", "web"],
};

const meta: ViewsResponse = {
  model: {
    name: "demo.item",
    description: "Demo item",
    rec_name: "name",
    order: "name",
    methods: [],
    access: { read: true, write: true, create: true, unlink: true },
  },
  fields: {
    name: { name: "name", type: "char", string: "Name", required: true, readonly: false, store: true },
    active: { name: "active", type: "boolean", string: "Active", required: false, readonly: false, store: true },
  },
  views: { list: { type: "list", model: "demo.item", columns: [{ field: "name" }, { field: "active" }] } },
};

const client = () => new QueryClient({ defaultOptions: { queries: { retry: false } } });

describe("French UI", () => {
  afterEach(() => setLanguage("en_US"));

  it("switches to the session language once signed in", async () => {
    mockFetch({
      "GET /api/v1/auth/session": () => session,
      "GET /api/v1/web/menus": () => [],
      "POST /api/v1/auth/logout": () => ({ ok: true }),
    });
    window.history.pushState({}, "", "/");
    render(<App queryClient={client()} />);
    expect(await screen.findByRole("heading", { name: "Bienvenue, Marie" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Aller au contenu" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Marie" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Se déconnecter" }));
    // the language stays French on the login page after signing out
    expect(await screen.findByRole("heading", { name: "Connexion à Tessera" })).toBeInTheDocument();
    expect(screen.getByLabelText("Mot de passe")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Se connecter" })).toBeInTheDocument();
  });

  it("renders list view buttons, counts and values in French", async () => {
    setLanguage("fr_FR");
    mockFetch({
      "GET /api/v1/models/demo.item": () => ({
        items: [
          { id: 1, name: "Un", active: true },
          { id: 2, name: "Deux", active: false },
        ],
        total: 2,
        limit: 80,
        offset: 0,
      }),
      "POST /api/v1/auth/logout": () => ({ ok: true }),
    });
    render(
      <QueryClientProvider client={client()}>
        <ListView model="demo.item" meta={meta} view={meta.views.list!} domain={[]} groupBy={[]} onOpen={vi.fn()} />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("2 enregistrements")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Colonnes" })).toBeInTheDocument();
    expect(screen.getByText("Oui")).toBeInTheDocument();
    expect(screen.getByText("Non")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox", { name: "Tout sélectionner" }));
    expect(screen.getByRole("toolbar", { name: "Actions groupées" })).toHaveTextContent("2 sélectionné(s)");
    for (const name of ["Exporter en CSV", "Archiver", "Désarchiver", "Supprimer"])
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
  });
});
