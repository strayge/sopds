(() => {
  "use strict";

  const sidebar = document.querySelector(".app-sidebar");
  const primaryNavigation = sidebar?.querySelector(".site-navigation");
  const languageControl = sidebar?.querySelector(".language-control");
  const secondaryLinks = [...(primaryNavigation?.querySelectorAll("a:nth-child(n+3)") || [])];
  if (!sidebar || !primaryNavigation || !languageControl || secondaryLinks.length === 0) return;

  const mobileQuery = matchMedia("(max-width: 48rem)");
  const actions = document.createElement("div");
  actions.className = "mobile-navigation-actions";

  function createMenu(id, label, iconClass, panelTag = "div") {
    const menu = document.createElement("div");
    menu.className = "mobile-navigation-menu";
    menu.dataset.mobileNavigationMenu = "";

    const toggle = document.createElement("button");
    toggle.className = "mobile-navigation-menu__toggle";
    toggle.type = "button";
    toggle.dataset.mobileNavigationMenuToggle = "";
    toggle.setAttribute("aria-controls", id);
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-label", label);

    const icon = document.createElement("span");
    icon.className = `mobile-navigation-menu__icon ${iconClass}`;
    icon.setAttribute("aria-hidden", "true");
    toggle.append(icon);

    const panel = document.createElement(panelTag);
    panel.id = id;
    panel.className = "mobile-navigation-menu__popover";
    menu.append(toggle, panel);
    return {menu, panel, toggle};
  }

  const languageLabel = languageControl.getAttribute("aria-label") || "Interface language";
  const languageMenu = createMenu(
    "mobile-language-menu", languageLabel, "mobile-navigation-menu__icon--language");
  const moreLabel = primaryNavigation.dataset.moreNavigationLabel || "More navigation";
  const moreMenu = createMenu(
    "mobile-secondary-navigation", moreLabel, "mobile-navigation-menu__icon--more", "nav");
  moreMenu.panel.classList.add("mobile-navigation-menu__links");
  moreMenu.panel.setAttribute("aria-label", moreLabel);
  actions.append(languageMenu.menu, moreMenu.menu);

  const menus = [languageMenu.menu, moreMenu.menu];

  function setOpen(menu, open) {
    menu.toggleAttribute("data-open", open);
    menu.querySelector("[data-mobile-navigation-menu-toggle]")
      ?.setAttribute("aria-expanded", String(open));
  }

  function closeMenus(except = null) {
    menus.forEach((menu) => {
      if (menu !== except) setOpen(menu, false);
    });
  }

  menus.forEach((menu) => {
    const toggle = menu.querySelector("[data-mobile-navigation-menu-toggle]");
    toggle?.addEventListener("click", () => {
      const open = !menu.hasAttribute("data-open");
      closeMenus(menu);
      setOpen(menu, open);
    });
  });

  function applyLayout(mobile) {
    closeMenus();
    const status = sidebar.querySelector("[data-selection-status]");
    if (mobile) {
      secondaryLinks.forEach((link) => moreMenu.panel.append(link));
      languageControl.classList.add("language-control--mobile");
      languageMenu.panel.append(languageControl);
      sidebar.insertBefore(actions, status);
    } else {
      secondaryLinks.forEach((link) => primaryNavigation.append(link));
      languageControl.classList.remove("language-control--mobile");
      sidebar.insertBefore(languageControl, status);
      actions.remove();
    }
  }

  sidebar.dataset.mobileNavigationReady = "";
  applyLayout(mobileQuery.matches);
  mobileQuery.addEventListener("change", (event) => applyLayout(event.matches));

  document.addEventListener("click", (event) => {
    if (!event.target.closest?.("[data-mobile-navigation-menu]")) closeMenus();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const openMenu = menus.find((menu) => menu.hasAttribute("data-open"));
    if (!openMenu) return;
    setOpen(openMenu, false);
    openMenu.querySelector("[data-mobile-navigation-menu-toggle]")?.focus();
  });
})();
