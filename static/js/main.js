// ─── Sidebar toggle ──────────────────────────────────────────────────────────
document.getElementById("toggle-sidebar")?.addEventListener("click", function(){
  document.body.classList.toggle("sidebar-open");
});
document.getElementById("sidebar-overlay")?.addEventListener("click", function(){
  document.body.classList.remove("sidebar-open");
});

// ─── Menus déroulants sidebar ────────────────────────────────────────────────
function toggleMenu(btn){
  btn.classList.toggle("open");
  const container = btn.nextElementSibling;
  if(container){
    container.classList.toggle("open");
  }
}

// ─── Changer de routeur ──────────────────────────────────────────────────────
function changerRouteur(id){
  if(id) window.location.href = "/parametres/routeurs/" + id + "/connecter";
}

// ─── Modal générique ─────────────────────────────────────────────────────────
let _modalCallback = null;

function ouvrirModal(titre, corps, callback){
  document.getElementById("modal-confirm-title").textContent = titre;
  document.getElementById("modal-confirm-body").textContent = corps;
  _modalCallback = callback;
  document.getElementById("modal-confirm").style.display = "flex";
}

function fermerModal(){
  document.getElementById("modal-confirm").style.display = "none";
  _modalCallback = null;
}

document.getElementById("modal-confirm-ok")?.addEventListener("click", function(){
  const cb = _modalCallback;
  fermerModal();
  if(cb) cb();
});

document.getElementById("modal-confirm")?.addEventListener("click", function(e){
  if(e.target === this) fermerModal();
});

// ─── Notifications toast ──────────────────────────────────────────────────────
function afficherNotif(message, type="info"){
  let container = document.getElementById("toast-container");
  if(!container){
    container = document.createElement("div");
    container.id = "toast-container";
    document.body.appendChild(container);
  }
  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 4000);
}

// ─── Reboot / Shutdown ───────────────────────────────────────────────────────
function confirmerRedemarrage(){
  ouvrirModal(
    "Redémarrer le routeur ?",
    "Le routeur sera inaccessible pendant quelques secondes.",
    () => {
      fetch("/systeme/redemarrer", {method:"POST"})
        .then(r => r.json())
        .then(d => {
          if(d.ok) afficherNotif("Redémarrage en cours...", "info");
          else afficherNotif(d.msg, "danger");
        });
    }
  );
}

function confirmerArrêt(){
  ouvrirModal(
    "Éteindre le routeur ?",
    "⚠️ Le routeur s'éteindra complètement !",
    () => {
      fetch("/systeme/eteindre", {method:"POST"})
        .then(r => r.json())
        .then(d => {
          if(d.ok) afficherNotif("Arrêt en cours...", "warning");
          else afficherNotif(d.msg, "danger");
        });
    }
  );
}

// ─── Sync ventes MikroTik → SQLite ───────────────────────────────────────────
function syncVentes(){
  return fetch("/api/sync-ventes", {method:"POST"})
    .then(r => r.json())
    .then(d => {
      if(d.ok && d.new > 0) afficherNotif(`${d.new} vente(s) synchronisée(s)`, "info");
    })
    .catch(() => {});
}

// ─── Auto-dismiss flash messages ─────────────────────────────────────────────
document.querySelectorAll(".flash").forEach(el => {
  setTimeout(() => el.remove(), 5000);
});
