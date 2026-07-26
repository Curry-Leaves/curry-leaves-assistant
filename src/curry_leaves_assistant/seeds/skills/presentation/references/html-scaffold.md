# html-scaffold — the deck plumbing, solved

This is the *structural* pattern only: slide switching, keyboard nav, progress, speaker
notes, print-to-PDF, light/dark. Take this so your effort goes into design and content —
then **replace the visual layer entirely**. The palette and type below are a deliberately
plain starting point, not a house style; every deck should look different.

Do not paste this and fill in text. Take the mechanics, design the rest.

## Non-negotiables it handles

- One slide visible at a time; `←` `→` `space` `PgUp` `PgDn` `Home` `End`, click, and swipe
- Progress indicator + slide counter
- `@media print` → one slide per page, no clipping, backgrounds preserved
- `prefers-color-scheme` light/dark, driven entirely by custom properties
- `prefers-reduced-motion` respected
- Speaker notes toggled with `S` (hidden by default, never printed)
- Scales to any projector via `clamp()` — no fixed pixel type

## Pattern

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Deck title</title>
<style>
  /* ---- design tokens: REDESIGN THESE PER DECK ---- */
  :root{
    --bg:#fbfaf8; --surface:#fff; --text:#14161a; --muted:#5b6472;
    --accent:#2f5fe0; --accent-2:#12a594; --line:#e3e6ea;
    --n1:#f4f6f8; --n2:#e8ecf1; --n3:#cbd3dd;
    --font:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --pad:clamp(2rem,6vw,5.5rem);
    --t-display:clamp(2.8rem,7vw,5.5rem);
    --t-head:clamp(1.9rem,3.6vw,3rem);
    --t-body:clamp(1rem,1.5vw,1.35rem);
    --t-cap:clamp(.8rem,1.05vw,.95rem);
  }
  @media (prefers-color-scheme:dark){
    :root{
      --bg:#0d1014; --surface:#151a21; --text:#eef2f6; --muted:#9aa6b6;
      --accent:#6f9bff; --accent-2:#2fd4bd; --line:#252c36;
      --n1:#171d25; --n2:#1f2731; --n3:#3a4553;
    }
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{background:var(--bg);color:var(--text);font-family:var(--font);
       font-size:var(--t-body);line-height:1.5;-webkit-font-smoothing:antialiased}

  .slide{position:fixed;inset:0;padding:var(--pad);display:none;
         flex-direction:column;justify-content:center;overflow:hidden}
  .slide.active{display:flex}
  .slide.active>*{animation:rise .32s cubic-bezier(.2,.7,.3,1) both}
  .slide.active>*:nth-child(2){animation-delay:.05s}
  .slide.active>*:nth-child(3){animation-delay:.1s}
  @keyframes rise{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
  @media (prefers-reduced-motion:reduce){
    .slide.active>*{animation:fade .2s both}
    @keyframes fade{from{opacity:0}to{opacity:1}}
  }

  h1{font-size:var(--t-display);line-height:1.02;letter-spacing:-.03em;margin:0 0 .5em;font-weight:700}
  h2{font-size:var(--t-head);line-height:1.1;letter-spacing:-.02em;margin:0 0 .6em;font-weight:650}
  p{max-width:62ch;margin:0 0 1em}
  .cap{font-size:var(--t-cap);color:var(--muted);letter-spacing:.08em;text-transform:uppercase}

  /* chrome */
  .prog{position:fixed;top:0;left:0;height:3px;background:var(--accent);
        width:0;transition:width .3s ease;z-index:10}
  .count{position:fixed;bottom:1.1rem;right:1.4rem;font-size:var(--t-cap);
         color:var(--muted);z-index:10;font-variant-numeric:tabular-nums}
  .notes{position:fixed;bottom:0;left:0;right:0;padding:1rem 1.4rem;
         background:var(--surface);border-top:1px solid var(--line);
         font-size:var(--t-cap);color:var(--muted);display:none;z-index:11}
  body.notes-on .notes.current{display:block}

  @media print{
    @page{size:1600px 900px landscape;margin:0}
    .slide{position:relative;display:flex!important;height:900px;width:1600px;
           page-break-after:always;break-after:page;inset:auto}
    .prog,.count,.notes{display:none!important}
    .slide.active>*{animation:none}
    *{-webkit-print-color-adjust:exact;print-color-adjust:exact}
  }
</style>
</head>
<body>
<div class="prog" id="prog"></div>
<div class="count" id="count"></div>

<section class="slide">
  <p class="cap">Section label</p>
  <h1>An asserting headline</h1>
  <p>Supporting line.</p>
</section>

<section class="slide">
  <h2>Next idea</h2>
  <!-- design each slide differently: full-bleed, split, focal, chart -->
</section>

<div class="notes" data-for="0">Speaker note for slide 1.</div>

<script>
(function(){
  var slides=[].slice.call(document.querySelectorAll('.slide')),
      notes=[].slice.call(document.querySelectorAll('.notes')),
      prog=document.getElementById('prog'), count=document.getElementById('count'), i=0;

  function show(n){
    i=Math.max(0,Math.min(slides.length-1,n));
    slides.forEach(function(s,k){s.classList.toggle('active',k===i)});
    notes.forEach(function(nt){nt.classList.toggle('current',+nt.dataset.for===i)});
    prog.style.width=((i+1)/slides.length*100)+'%';
    count.textContent=(i+1)+' / '+slides.length;
    if(location.hash!=='#'+(i+1)) history.replaceState(null,'','#'+(i+1));
  }
  document.addEventListener('keydown',function(e){
    var k=e.key;
    if(k==='ArrowRight'||k==='PageDown'||k===' '){e.preventDefault();show(i+1)}
    else if(k==='ArrowLeft'||k==='PageUp'){e.preventDefault();show(i-1)}
    else if(k==='Home'){show(0)} else if(k==='End'){show(slides.length-1)}
    else if(k==='s'||k==='S'){document.body.classList.toggle('notes-on')}
  });
  document.addEventListener('click',function(e){
    if(e.target.closest('a,button'))return;
    show(i + (e.clientX < innerWidth*0.25 ? -1 : 1));
  });
  var x0=null;
  document.addEventListener('touchstart',function(e){x0=e.touches[0].clientX},{passive:true});
  document.addEventListener('touchend',function(e){
    if(x0===null)return; var dx=e.changedTouches[0].clientX-x0;
    if(Math.abs(dx)>50) show(i+(dx<0?1:-1)); x0=null;
  },{passive:true});

  var start=parseInt((location.hash||'').slice(1),10);
  show(isFinite(start)&&start>0?start-1:0);
})();
</script>
</body>
</html>
```

## Notes

- **Print `@page` size** is set to a 16:9 pixel box so PDF export matches slide
  proportions. Keep `print-color-adjust:exact` or backgrounds drop out of the PDF.
- **Slide deep-links** — the hash tracks the slide, so a shared link can open at a slide.
- **Click-to-advance** ignores clicks on links/buttons; the left quarter goes back.
- **Every color must be a custom property.** Hard-coding `#111` in an SVG fill is the most
  common dark-mode bug — use `currentColor` or `var(--…)` in every fill and stroke. See
  `visual-craft.md`.
- Keep the JS this small. Anything larger is design effort spent in the wrong place.
