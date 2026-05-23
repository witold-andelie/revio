const express = require('express');
const mysql = require('mysql2');
const app = express();

const db = mysql.createConnection({
  host: 'localhost',
  user: 'root',
  password: 'admin123',
  database: 'app'
});

// SQL injection: user input interpolated into query
app.get('/user/:id', (req, res) => {
  const id = req.params.id;
  const query = `SELECT * FROM users WHERE id = ${id}`;
  db.query(query, (err, results) => res.json(results));
});

// eval on untrusted input
app.post('/calc', express.json(), (req, res) => {
  const expr = req.body.expr;
  const result = eval(expr);
  res.json({ result });
});

// XSS via direct HTML
app.get('/hello', (req, res) => {
  const name = req.query.name;
  res.send(`<h1>Hello ${name}!</h1>`);
});

app.listen(3000);
