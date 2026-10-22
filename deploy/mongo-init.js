db = db.getSiblingDB('hedgehoog');
if (!db.getUser('hedgehoog')) {
  db.createUser({
    user: 'hedgehoog',
    pwd: process.env.MONGO_APP_PASSWORD,
    roles: [{ role: 'readWrite', db: 'hedgehoog' }],
  });
}
