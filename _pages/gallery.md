---
layout: page
title: Gallery
permalink: /gallery/
nav: true
nav_order: 4
images:
  lightbox2: true
---

<style>
  .post > .post-header,
  .al-lightbox-caption {
    display: none;
  }

  .photo-wall {
    column-count: 3;
    column-gap: 12px;
  }

  .photo-wall figure {
    break-inside: avoid;
    margin: 0 0 12px;
  }

  .photo-wall img {
    display: block;
    width: 100%;
    height: auto;
    border-radius: 8px;
  }

  @media (max-width: 768px) {
    .photo-wall {
      column-count: 2;
    }
  }

  @media (max-width: 480px) {
    .photo-wall {
      column-count: 1;
    }
  }
</style>

<div class="photo-wall">
  {% for photo in site.data.gallery %}
    <figure>
      <a
        href="{{ photo.full }}"
        data-lightbox="photography"
        data-title="{{ photo.caption | default: photo.alt | escape }}"
      >
        <img
          src="{{ photo.thumb }}"
          {% if photo.width and photo.height %}
            width="{{ photo.width }}"
            height="{{ photo.height }}"
          {% endif %}
          alt="{{ photo.alt | escape }}"
          loading="lazy"
          decoding="async"
        >
      </a>
    </figure>
  {% endfor %}
</div>

<script>
  (() => {
    const gallery = document.querySelector(".photo-wall");
    if (!gallery) return;

    gallery.querySelectorAll("img").forEach((image) => {
      const removePhoto = () => image.closest("figure")?.remove();
      image.addEventListener("error", removePhoto, { once: true });
      if (image.complete && image.naturalWidth === 0) removePhoto();
    });

    const photos = Array.from(gallery.children);
    for (let index = photos.length - 1; index > 0; index -= 1) {
      const randomIndex = Math.floor(Math.random() * (index + 1));
      [photos[index], photos[randomIndex]] = [photos[randomIndex], photos[index]];
    }

    const fragment = document.createDocumentFragment();
    photos.forEach((photo) => fragment.appendChild(photo));
    gallery.appendChild(fragment);
  })();
</script>
