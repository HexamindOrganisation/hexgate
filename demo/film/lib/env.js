// What `./demo/run film` passes to a scene script. Defaults match a local
// `./demo/run <demo>`, so a scene can also run by hand: node devops/01-unsafe.js
const path = require('path');

const env = {
  out: process.env.FILM_OUT || path.resolve(__dirname, '../../.run/film/takes'),
  app: process.env.FILM_APP_URL || 'http://localhost:3000',
  dash: process.env.FILM_DASH_URL || 'http://localhost:8000',
  orgId: process.env.FILM_ORG_ID || '',
  projectId: process.env.FILM_PROJECT_ID || '',
};

// A scene script's body: run it, print the raw video path, fail loudly.
function scene(fn) {
  fn()
    .then((video) => console.log(video))
    .catch((e) => {
      console.error(e);
      process.exit(1);
    });
}

module.exports = { env, scene };
