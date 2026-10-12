(()=>{
  const toggle=document.querySelector('.menu-toggle');
  const links=document.querySelector('.nav-links');
  if(toggle&&links){
    const close=()=>{links.classList.remove('open');toggle.setAttribute('aria-expanded','false');toggle.setAttribute('aria-label','Open navigation');toggle.textContent='☰'};
    toggle.addEventListener('click',()=>{const open=links.classList.toggle('open');toggle.setAttribute('aria-expanded',String(open));toggle.setAttribute('aria-label',open?'Close navigation':'Open navigation');toggle.textContent=open?'×':'☰'});
    links.querySelectorAll('a').forEach(a=>a.addEventListener('click',close));
    document.addEventListener('keydown',e=>{if(e.key==='Escape')close()});
    document.addEventListener('click',e=>{if(!links.contains(e.target)&&!toggle.contains(e.target))close()});
    window.addEventListener('resize',()=>{if(window.matchMedia('(min-width: 701px)').matches)close()});
  }
  document.querySelectorAll('[data-year]').forEach(el=>el.textContent=new Date().getFullYear());
  const reduce=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const items=document.querySelectorAll('.reveal');
  if('IntersectionObserver'in window&&!reduce){
    const observer=new IntersectionObserver(entries=>entries.forEach(entry=>{if(entry.isIntersecting){entry.target.classList.add('visible');observer.unobserve(entry.target)}}),{threshold:.12});
    items.forEach((el,i)=>{el.style.transitionDelay=(i%3)*70+'ms';observer.observe(el)});
  }else items.forEach(el=>el.classList.add('visible'));
  const manual=document.querySelector('#manual-hours');
  const board=document.querySelector('#board-hours');
  const manualOut=document.querySelector('#manual-output');
  const boardOut=document.querySelector('#board-output');
  const saved=document.querySelector('#saved-percent');
  const calc=()=>{
    if(!manual||!board||!manualOut||!boardOut||!saved)return;
    const before=Number(manual.value),after=Number(board.value);
    manualOut.value=before;boardOut.value=after;
    saved.textContent=`${Math.max(0,Math.round((before-after)/before*100))}%`;
  };
  manual?.addEventListener('input',calc);board?.addEventListener('input',calc);calc();
})();

