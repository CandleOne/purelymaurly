// Simple client-side cart using localStorage
(function(){
  const STORAGE_KEY = 'pm_cart';

  function loadCart() {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || []; }
    catch { return []; }
  }
  function saveCart(cart) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(cart));
  }
  function findIndex(cart, id) { return cart.findIndex(i => i.id === id); }
  function totalItems(cart){ return cart.reduce((s,i)=>s + (i.qty||0), 0); }
  function subtotal(cart){ return cart.reduce((s,i)=> s + (i.price * i.qty), 0); }

  // Badge
  function updateBadge() {
    const count = totalItems(loadCart());
    // Drawer header badge
    const drawerBadge = document.getElementById('cartCount');
    if (drawerBadge) {
      drawerBadge.textContent = String(count);
      drawerBadge.style.display = count > 0 ? 'inline-flex' : 'none';
    }
    // Header icon badges
    document.querySelectorAll('[data-cart-count]').forEach(el => {
      el.textContent = String(count);
      el.style.display = count > 0 ? 'inline-flex' : 'none';
    });
  }

  // Drawer open/close
  const overlay = document.getElementById('cartOverlay');
  const drawer = document.getElementById('cartDrawer');
  function openCart() {
    if (!overlay || !drawer) return;
    overlay.classList.add('open');
    drawer.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    renderCart();
  }
  function closeCart() {
    if (!overlay || !drawer) return;
    overlay.classList.remove('open');
    drawer.classList.remove('open');
    drawer.setAttribute('aria-hidden', 'true');
  }

  // Render cart
  function renderCart(){
    const list = document.getElementById('cartItems');
  const sub = document.getElementById('cartSubtotal');
    if (!list || !sub) return;
    const cart = loadCart();
    list.innerHTML = '';
    if (cart.length === 0) {
      list.innerHTML = '<div class="cart-empty">Your cart is empty.</div>';
    } else {
      cart.forEach(item => {
        const row = document.createElement('div');
        row.className = 'cart-item';
        row.innerHTML = `
          <img class="cart-item-img" src="${item.image}" alt="${item.name}">
          <div class="cart-item-info">
            <div class="cart-item-title">${item.name}</div>
            <div class="cart-item-price">$${item.price.toFixed(2)}</div>
            <div class="cart-item-qty">
              <button class="qty-btn" data-action="dec" data-id="${item.id}" aria-label="Decrease quantity">-</button>
              <span class="qty-num">${item.qty}</span>
              <button class="qty-btn" data-action="inc" data-id="${item.id}" aria-label="Increase quantity">+</button>
            </div>
          </div>
          <button class="remove-item" data-action="remove" data-id="${item.id}" aria-label="Remove item">×</button>
        `;
        list.appendChild(row);
      });
    }
    sub.textContent = `$${subtotal(cart).toFixed(2)}`;
    const checkoutBtn = document.querySelector('.checkout-btn');
    if (checkoutBtn){
      checkoutBtn.disabled = cart.length === 0;
      checkoutBtn.addEventListener('click', (e)=>{ e.preventDefault(); closeCart(); window.location.href='checkout.html'; });
    }
    updateBadge();
  }

  // Cart operations
  const Cart = {
    add(product) {
      const cart = loadCart();
      const idx = findIndex(cart, product.id);
      if (idx >= 0) {
        cart[idx].qty += product.qty || 1;
      } else {
        cart.push({ id: product.id, name: product.name, price: product.price, image: product.image, qty: product.qty || 1 });
      }
      saveCart(cart); updateBadge();
    },
    inc(id){
      const cart = loadCart();
      const idx = findIndex(cart, id); if (idx < 0) return;
      cart[idx].qty += 1; saveCart(cart); renderCart();
    },
    dec(id){
      const cart = loadCart();
      const idx = findIndex(cart, id); if (idx < 0) return;
      cart[idx].qty = Math.max(1, cart[idx].qty - 1); saveCart(cart); renderCart();
    },
    remove(id){
      let cart = loadCart();
      cart = cart.filter(i => i.id !== id);
      saveCart(cart); renderCart();
    },
    open: openCart,
    close: closeCart,
  };
  window.PMCart = Cart;

  // Wire UI
  function onReady(){
    // Badge update on load
    updateBadge();

    // Open cart from header icon
    const cartBtn = document.querySelector('.cart-button');
    if (cartBtn) {
      cartBtn.addEventListener('click', (e) => { e.preventDefault(); openCart(); });
    }

    // Overlay/close
    if (overlay) overlay.addEventListener('click', closeCart);
    const closeBtn = document.getElementById('closeCart');
    if (closeBtn) closeBtn.addEventListener('click', closeCart);
    document.addEventListener('keydown', (e)=>{ if (e.key === 'Escape') closeCart(); });

    // List actions (qty/remove)
    const list = document.getElementById('cartItems');
    if (list) {
      list.addEventListener('click', (e) => {
        const btn = e.target.closest('button'); if (!btn) return;
        const id = btn.getAttribute('data-id');
        const action = btn.getAttribute('data-action');
        if (!id || !action) return;
        if (action === 'inc') Cart.inc(id);
        else if (action === 'dec') Cart.dec(id);
        else if (action === 'remove') Cart.remove(id);
      });
    }

    // Add-to-cart buttons
    document.querySelectorAll('.add-to-cart').forEach(btn => {
      btn.addEventListener('click', () => {
        const id = btn.dataset.id || btn.closest('.product-card')?.querySelector('.product-title')?.textContent?.toLowerCase().replace(/\s+/g,'-') || String(Date.now());
        const name = btn.dataset.name || btn.closest('.product-card')?.querySelector('.product-title')?.textContent || 'Product';
        const price = parseFloat(btn.dataset.price || btn.closest('.product-card')?.querySelector('.price')?.textContent?.replace(/[^0-9.]/g,'') || '0') || 0;
        const image = btn.dataset.image || btn.closest('.product-card')?.querySelector('img')?.getAttribute('src') || '';
        Cart.add({ id, name, price, image, qty: 1 });
      });
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', onReady);
  else onReady();
})();
